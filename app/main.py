"""FastAPI 入口：签发 / 管理 / 消费演示接口 + Web 生成器页面。

启动：uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import keys
from .db import get_conn, init_db
from .middleware import (
    _quota_error,
    remaining_headers,
    require_key,
    reserve_for_request,
)
from .providers import Provider, registry
from .quota import refund, settle
from .security import (
    COOKIE_NAME,
    SESSION_TTL,
    admin_token,
    issue_session_cookie,
    rate_limit_issue,
    rate_limit_login,
    require_admin,
)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="API Key 生成器",
    description="多维度限额 key：项目/共享池/计费/模型绑定，预扣+退差结算，转发任意 OpenAI 兼容上游（含流式）",
    version="0.4.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- 请求模型 ----------


class LimitsBody(BaseModel):
    calls: int | None = Field(default=None, description="额定次数，留空不限")
    tokens: int | None = Field(default=None, description="额定 token 数，留空不限")
    expires: str | None = Field(
        default=None, description="有效期，如 '30d' / '12h'，留空永久"
    )
    cycle: str | None = Field(
        default=None, description="周期重置：monthly / daily / hourly，留空不重置"
    )
    provider: str | None = Field(
        default=None, description="绑定服务商 id（需已配置），留空不限"
    )
    model: str | None = Field(default=None, description="绑定模型名，留空不限")
    project_id: str | None = Field(default=None, description="归属项目，留空无项目")
    rps_limit: int | None = Field(default=None, ge=1, description="按 key 限流，留空继承全局")
    note: str | None = None


class RechargeBody(BaseModel):
    calls: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)


class ProjectBody(BaseModel):
    name: str = Field(min_length=1)
    owner: str | None = None
    calls: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)
    balance: float | None = Field(default=None, ge=0)
    note: str | None = None


class ProjectRechargeBody(BaseModel):
    calls: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)
    balance: float | None = Field(default=None, ge=0)


class PricingBody(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    price_per_million: float = Field(ge=0, description="元 / 1M tokens")


class ChatBody(BaseModel):
    prompt: str = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=1_000_000)
    provider: str | None = Field(default=None, description="指定服务商，默认取 key 绑定或默认服务商")
    model: str | None = Field(default=None, description="指定模型，默认取 key 绑定或服务商默认模型")
    stream: bool = Field(default=False, description="true 时返回 SSE 流式响应")


# ---------- 管理登录（HttpOnly 签名会话 Cookie）----------


class LoginBody(BaseModel):
    token: str = Field(min_length=1)


@app.post("/admin/login")
def admin_login(
    body: LoginBody,
    response: Response,
    request: Request,
    _: None = Depends(rate_limit_login),
) -> dict[str, Any]:
    """用 ADMIN_TOKEN 换 HttpOnly 会话 cookie（12h，签名防伪造；登录限流防暴力）。"""
    expected = admin_token()
    if expected is None:
        raise HTTPException(403, "KEYTOOL_ADMIN_TOKEN 未配置，无需登录")
    import hmac

    if not hmac.compare_digest(body.token, expected):
        raise HTTPException(401, "invalid admin token")
    # HTTPS 或显式开启时标记 Secure（防中间人嗅探会话）
    secure = request.url.scheme == "https" or os.environ.get("KEYTOOL_FORCE_SECURE_COOKIE") == "1"
    response.set_cookie(
        COOKIE_NAME,
        issue_session_cookie(expected),
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=SESSION_TTL,
        path="/",
    )
    return {"ok": True, "expires_in": SESSION_TTL}


@app.post("/admin/logout")
def admin_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/admin/me")
def admin_me(request: Request) -> dict[str, Any]:
    """返回当前管理身份状态（页面据此显示登录/未登录）。"""
    try:
        require_admin(request)
        return {"admin": True}
    except HTTPException:
        return {"admin": False}


# ---------- 签发 / 管理 ----------


@app.post("/keys", status_code=201)
def create_key(
    body: LimitsBody,
    request: Request,
    _: None = Depends(rate_limit_issue),
    __: None = Depends(require_admin),
) -> dict[str, Any]:
    """签发 key（需管理令牌）：明文 token 只返回这一次。"""
    try:
        issued = keys.issue(
            keys.KeyLimits(
                calls=body.calls,
                tokens=body.tokens,
                expires=body.expires,
                cycle=body.cycle,
                provider=body.provider,
                model=body.model,
                project_id=body.project_id,
                rps_limit=body.rps_limit,
                note=body.note,
            )
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "id": issued.id,
        "key": issued.token,  # 仅此一次，不再可查
        "token_hash_prefix": issued.token_hash_prefix,
        "limits": issued.limits,
        "warning": "明文 key 只显示这一次，请立即保存",
    }


# ---------- 项目（共享额度池 + 余额）----------


@app.post("/projects", status_code=201)
def create_project(body: ProjectBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    try:
        return keys.create_project(
            name=body.name, owner=body.owner, calls=body.calls,
            tokens=body.tokens, balance=body.balance, note=body.note,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/projects")
def list_projects(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"items": keys.list_projects()}


@app.get("/projects/{project_id}")
def get_project(project_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    p = keys.project_status(project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


@app.post("/projects/{project_id}/recharge")
def recharge_project(
    project_id: str, body: ProjectRechargeBody, _: None = Depends(require_admin)
) -> dict[str, Any]:
    try:
        ok = keys.recharge_project(
            project_id, calls=body.calls, tokens=body.tokens, balance=body.balance
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not ok:
        raise HTTPException(404, "project not found or archived")
    return {"id": project_id, "recharged": body.model_dump(exclude_none=True)}


# ---------- 定价 ----------


@app.get("/pricing")
def list_pricing(_: None = Depends(require_admin)) -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM pricing ORDER BY provider, model").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/pricing", status_code=201)
def upsert_pricing(body: PricingBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO pricing (provider, model, price_per_million) VALUES (?,?,?) "
            "ON CONFLICT(provider, model) DO UPDATE SET price_per_million = excluded.price_per_million",
            (body.provider, body.model, body.price_per_million),
        )
    return {"provider": body.provider, "model": body.model,
            "price_per_million": body.price_per_million}


# ---------- 用量报表 ----------


@app.get("/stats/usage")
def usage_stats(
    project_id: str | None = None,
    days: int = 30,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """用量统计（需管理令牌）：按天聚合成功消费的 calls / tokens / 金额。"""
    from .stats import project_totals, usage_stats as _usage_stats

    data = _usage_stats(project_id=project_id, days=days)
    data["project_totals"] = project_totals(project_id=project_id)
    return data


@app.get("/providers")
def list_providers() -> dict[str, Any]:
    """列出已配置的服务商（不暴露 api_key），含预设模型列表与补 key 提示。"""
    from . import provider_store

    return {
        "default": registry.default_id,
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "default_model": p.default_model,
                "models": p.models,
                "needs_key": not bool(p.api_key),  # api_key 未配置/环境变量未设置
            }
            for p in registry.all()
        ],
        "presets": provider_store.presets_missing(),  # 未添加的内置预设（一键添加）
    }


class ProviderKeyBody(BaseModel):
    api_key: str = Field(min_length=1, description="明文 api key（服务端加密存储，支持 ${ENV}）")


@app.post("/providers/{provider_id}/key")
def update_provider_key(provider_id: str, body: ProviderKeyBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    """只更新服务商 api_key（页面「填 key」用），不影响其他配置。"""
    from . import provider_store

    if not provider_store.update_key(provider_id, body.api_key):
        raise HTTPException(404, "provider not found")
    registry.reload()
    return {"id": provider_id, "updated": True}


@app.post("/providers/presets")
def add_provider_presets(_: None = Depends(require_admin)) -> dict[str, Any]:
    """一键添加尚未配置的内置预设服务商。"""
    from . import provider_store

    added = provider_store.seed_presets()
    registry.reload()
    return {"added": added, "total": len(registry.all())}


class ProviderBody(BaseModel):
    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1, description="明文 api key（服务端加密存储）")
    default_model: str | None = None
    models: list[str] = []
    is_default: bool = False


@app.post("/providers", status_code=201)
def create_provider(body: ProviderBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    from . import provider_store

    models = list(body.models) or ([body.default_model] if body.default_model else [])
    p = Provider(id=body.id, name=body.name, base_url=body.base_url.rstrip("/"),
                 api_key=body.api_key, default_model=body.default_model or "", models=models)
    provider_store.upsert(p, is_default=body.is_default)
    registry.reload()
    return {"id": body.id, "name": body.name, "default_model": body.default_model,
            "models": models, "encrypted": provider_store.encryption_enabled()}


@app.put("/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderBody, _: None = Depends(require_admin)) -> dict[str, Any]:
    from . import provider_store

    if provider_id != body.id:
        raise HTTPException(422, "id 不可修改（删除后重建）")
    models = list(body.models) or ([body.default_model] if body.default_model else [])
    p = Provider(id=body.id, name=body.name, base_url=body.base_url.rstrip("/"),
                 api_key=body.api_key, default_model=body.default_model or "", models=models)
    provider_store.upsert(p, is_default=body.is_default)
    registry.reload()
    return {"id": body.id, "updated": True}


@app.delete("/providers/{provider_id}")
def delete_provider(provider_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    from . import provider_store

    if not provider_store.delete(provider_id):
        raise HTTPException(404, "provider not found")
    registry.reload()
    return {"id": provider_id, "deleted": True}


@app.post("/providers/reload")
def reload_providers(_: None = Depends(require_admin)) -> dict[str, Any]:
    """从 DB 重载服务商注册表（改完即时生效）。"""
    registry.reload()
    return {"reloaded": len(registry.all()), "default": registry.default_id}


@app.get("/keys")
def list_all_keys(
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """列出所有 key（需管理令牌），不含 token_hash。"""
    rows, total = keys.list_keys(
        status_filter=status, project_id=project_id, limit=limit, offset=offset
    )
    return {"total": total, "items": rows}


@app.get("/keys/{key_id}")
def get_status(key_id: str) -> dict[str, Any]:
    """查余量：各维度已用 / 总额 / 剩余 / 有效期。"""
    st = keys.status(key_id)
    if st is None:
        raise HTTPException(404, "key not found")
    return st


@app.get("/keys/{key_id}/logs")
def get_key_logs(
    key_id: str,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """查某个 key 的审计日志（需管理令牌）。"""
    rows, total = keys.logs(key_id, limit=limit, offset=offset)
    if total == -1:
        raise HTTPException(404, "key not found")
    return {"total": total, "items": rows}


@app.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    """吊销 key（需管理令牌）。"""
    if not keys.revoke(key_id):
        raise HTTPException(404, "key not found or already revoked")
    return {"id": key_id, "status": "revoked"}


@app.post("/keys/{key_id}/rotate")
def rotate_key(key_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    """吊销重签：旧 key 立即失效，按原限额（扣除已用量）签发新 key。"""
    issued = keys.rotate(key_id)
    if issued is None:
        raise HTTPException(404, "key not found or already revoked")
    return {
        "id": issued.id,
        "key": issued.token,  # 明文仅此一次
        "token_hash_prefix": issued.token_hash_prefix,
        "limits": issued.limits,
        "warning": "旧 key 已吊销；新 key 明文只显示这一次",
    }


@app.post("/keys/{key_id}/recharge")
def recharge_key(
    key_id: str, body: RechargeBody, _: None = Depends(require_admin)
) -> dict[str, Any]:
    """充值（需管理令牌）。"""
    try:
        ok = keys.recharge(key_id, calls=body.calls, tokens=body.tokens)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not ok:
        raise HTTPException(404, "key not found or revoked")
    return {"id": key_id, "recharged": body.model_dump(exclude_none=True)}


# ---------- 消费：key 校验 + 预扣 + 转发上游 + 退差 ----------


def _resolve_route(
    token: str, provider: str | None, model: str | None
) -> tuple[Provider, str]:
    """决定本次调用走哪个服务商/模型。

    优先级：请求显式指定 > key 绑定 > 默认服务商。
    请求指定与 key 绑定冲突 → 403；服务商不存在/未配置 → 503。
    """
    from .keys import sha256

    with get_conn() as conn:
        row = conn.execute(
            "SELECT provider, model FROM api_keys WHERE token_hash = ?",
            (sha256(token),),
        ).fetchone()
    key_provider = row["provider"] if row else None
    key_model = row["model"] if row else None

    if provider and key_provider and provider != key_provider:
        raise HTTPException(403, f"provider not allowed by key (bound to {key_provider})")
    if model and key_model and model != key_model:
        raise HTTPException(403, f"model not allowed by key (bound to {key_model})")

    provider_id = provider or key_provider
    if provider_id:
        p = registry.get(provider_id)
        if p is None:
            raise HTTPException(503, f"服务商 {provider_id} 不存在或已被删除")
    else:
        p = registry.default()
    if p is None:
        raise HTTPException(503, "no provider configured")
    resolved_model = model or key_model or p.default_model
    return p, resolved_model


async def _call_upstream(
    req: Request,
    provider: Provider,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> httpx.Response:
    """转发上游；网络错误/上游 4xx/5xx → 全额退款并抛 502。"""
    res = req.state.reservation
    client = _client_for(provider)
    try:
        upstream = await client.post(
            f"{provider.base_url}/chat/completions", headers=headers, json=payload
        )
    except httpx.HTTPError as e:
        await run_in_threadpool(refund, res.reservation_id)
        raise HTTPException(502, f"upstream unreachable: {e.__class__.__name__}")
    if upstream.status_code >= 400:
        await run_in_threadpool(refund, res.reservation_id)  # 上游错误不算用户消费
        raise HTTPException(502, f"upstream {upstream.status_code}: {_upstream_error_detail(upstream)}")
    return upstream


def _upstream_error_detail(resp: httpx.Response) -> str:
    detail = "upstream error"
    try:
        detail = resp.json().get("error", {}).get("message", detail)
    except Exception:
        pass
    return detail


_upstream_client: httpx.AsyncClient | None = None


def _get_upstream_client() -> httpx.AsyncClient:
    """共享上游 HTTP 客户端（httpx 建议复用；trust_env=False 避开 Windows 代理探测开销）。"""
    global _upstream_client
    if _upstream_client is None or _upstream_client.is_closed:
        _upstream_client = httpx.AsyncClient(timeout=60.0, trust_env=False)
    return _upstream_client


def _client_for(provider: Provider) -> httpx.AsyncClient:
    """测试注入 transport 时用独立 client；生产走共享客户端。"""
    if provider.transport is not None:
        return httpx.AsyncClient(timeout=60.0, trust_env=False, transport=provider.transport)
    return _get_upstream_client()


async def _close_upstream_client(client: httpx.AsyncClient, provider: Provider) -> None:
    """只关闭测试注入的独立 client；共享客户端保持复用。"""
    if provider.transport is not None:
        await client.aclose()


@app.post("/v1/chat")
async def chat_completion(request: Request, body: ChatBody):
    """消费：校验 key → 解析路由 → 预扣（token+金额）→ 转发 → 按 usage 退差。

    上游 2xx：按 usage.total_tokens 结算退差（key/项目池/余额同步）；
    上游 4xx/5xx 或网络错误：全额退款（不扣用户），返回 502；
    stream=true 时 SSE 透传，结束时按上游 usage 退差；
    超时未结算：按预扣留存。
    """
    token = request.headers.get("X-Api-Key", "")

    @require_key()
    async def _handler(req: Request):
        provider, model = await run_in_threadpool(_resolve_route, token, body.provider, body.model)

        res = await reserve_for_request(req, reserve_tokens=body.max_tokens,
                                  provider=provider.id, model=model)
        if not res.ok:
            raise _quota_error(res)

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": body.prompt}],
            "max_tokens": body.max_tokens,
            "stream": body.stream,
        }
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        try:
            if body.stream:
                return await _stream_chat(req, provider, payload, headers)
            return await _non_stream_chat(req, provider, payload, headers, token)
        except Exception:
            # 意外异常兜底：全额退款（上游明确错误已在分支内 refund，幂等）
            await run_in_threadpool(refund, res.reservation_id)
            raise

    return await _handler(request)


async def _non_stream_chat(
    req: Request,
    provider: Provider,
    payload: dict[str, Any],
    headers: dict[str, str],
    token: str,
) -> JSONResponse:
    upstream = await _call_upstream(req, provider, payload, headers)
    data = upstream.json()
    usage_tokens = int(data.get("usage", {}).get("total_tokens", 0))
    await run_in_threadpool(settle, req.state.reservation.key_id,
                             req.state.reservation.reservation_id, usage_tokens)
    return JSONResponse(
        {
            "id": data.get("id", "unknown"),
            "reply": data["choices"][0]["message"]["content"],
            "model": data.get("model", payload["model"]),
            "usage": data.get("usage"),
        },
        headers=await run_in_threadpool(remaining_headers, token),
    )


# ---------- OpenAI 兼容端点（/v1/chat/completions）----------


class OpenAICompletionsBody(BaseModel):
    """OpenAI 兼容请求体：messages 数组 + 常用采样参数，其余参数透传（白名单）。"""

    model_config = {"extra": "allow"}

    model: str | None = None
    messages: list[dict[str, Any]] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    stream: bool = False

    def upstream_payload(self, resolved_model: str) -> dict[str, Any]:
        """只透传白名单内的采样参数（防误传字段污染上游）。"""
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": self.messages,
            "stream": self.stream,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        full = self.model_dump(exclude_none=True)
        for k in _SAMPLING_PARAMS:
            if k in full:
                payload[k] = full[k]
        return payload


# 允许透传上游的 OpenAI 采样参数（白名单）
_SAMPLING_PARAMS = {
    "temperature", "top_p", "stop", "max_completion_tokens", "n",
    "frequency_penalty", "presence_penalty", "seed", "user",
    "response_format", "tools", "tool_choice", "logprobs", "top_logprobs",
}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: OpenAICompletionsBody):
    """OpenAI 兼容消费端点：OpenAI SDK / 任意客户端可直接接入。

    鉴权：`Authorization: Bearer <key>`；请求/响应均为 OpenAI chat 格式；
    `stream: true` 返回 OpenAI SSE；计费/额度语义与 /v1/chat 一致。
    """
    token = request.headers.get("X-Api-Key", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            token = auth[7:]

    @require_key()
    async def _handler(req: Request):
        provider, model = await run_in_threadpool(_resolve_route, token, None, body.model)

        pre_tokens = body.max_tokens or 4096  # 未指定时按 4096 预扣，实际以 usage 结算
        res = await reserve_for_request(req, reserve_tokens=pre_tokens,
                                  provider=provider.id, model=model)
        if not res.ok:
            raise _quota_error(res)

        payload = body.upstream_payload(model)
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        try:
            if body.stream:
                return await _stream_openai(req, provider, payload, headers)
            return await _non_stream_openai(req, provider, payload, headers, token)
        except Exception:
            await run_in_threadpool(refund, res.reservation_id)
            raise

    return await _handler(request)


async def _non_stream_openai(
    req: Request,
    provider: Provider,
    payload: dict[str, Any],
    headers: dict[str, str],
    token: str,
) -> JSONResponse:
    import time as _time

    upstream = await _call_upstream(req, provider, payload, headers)
    data = upstream.json()
    usage_tokens = int(data.get("usage", {}).get("total_tokens", 0))
    await run_in_threadpool(settle, req.state.reservation.key_id,
                             req.state.reservation.reservation_id, usage_tokens)
    resp = {
        "id": data.get("id", "chatcmpl-" + req.state.reservation.reservation_id[:12]),
        "object": "chat.completion",
        "created": int(_time.time()),
        "model": data.get("model", payload["model"]),
        "choices": data.get("choices", []),
        "usage": data.get("usage"),
    }
    return JSONResponse(resp, headers=await run_in_threadpool(remaining_headers, token))


async def _stream_openai(
    req: Request,
    provider: Provider,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> StreamingResponse:
    """OpenAI SSE 透传（上游即 OpenAI 格式），结束时按 usage 结算退差。"""
    res = req.state.reservation
    client = _client_for(provider)
    try:
        stream = client.stream(
            "POST", f"{provider.base_url}/chat/completions",
            headers=headers, json=payload,
        )
        upstream = await stream.__aenter__()
    except httpx.HTTPError as e:
        await _close_upstream_client(client, provider)
        await run_in_threadpool(refund, res.reservation_id)
        raise HTTPException(502, f"upstream unreachable: {e.__class__.__name__}")

    if upstream.status_code >= 400:
        detail = _upstream_error_detail(upstream)
        await stream.__aexit__(None, None, None)
        await _close_upstream_client(client, provider)
        await run_in_threadpool(refund, res.reservation_id)
        raise HTTPException(502, f"upstream {upstream.status_code}: {detail}")

    async def gen():
        usage_tokens: int | None = None
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            obj = json.loads(data)
                            u = obj.get("usage") or {}
                            if u.get("total_tokens"):
                                usage_tokens = int(u["total_tokens"])
                        except Exception:
                            pass
                    yield line + "\n\n"
        except Exception:
            pass
        finally:
            await stream.__aexit__(None, None, None)
            await _close_upstream_client(client, provider)
            if usage_tokens is not None:
                await run_in_threadpool(settle, res.key_id, res.reservation_id, usage_tokens)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _stream_chat(
    req: Request,
    provider: Provider,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> StreamingResponse:
    """SSE 透传：逐行转发上游，解析 usage 用于结束时结算。

    客户端中途断开或上游未返回 usage → 按预扣留存（不主动退）。
    """
    res = req.state.reservation
    client = _client_for(provider)
    try:
        stream = client.stream(
            "POST",
            f"{provider.base_url}/chat/completions",
            headers=headers,
            json=payload,
        )
        upstream = await stream.__aenter__()
    except httpx.HTTPError as e:
        await _close_upstream_client(client, provider)
        await run_in_threadpool(refund, res.reservation_id)
        raise HTTPException(502, f"upstream unreachable: {e.__class__.__name__}")

    if upstream.status_code >= 400:
        detail = _upstream_error_detail(upstream)
        await stream.__aexit__(None, None, None)
        await _close_upstream_client(client, provider)
        await run_in_threadpool(refund, res.reservation_id)
        raise HTTPException(502, f"upstream {upstream.status_code}: {detail}")

    async def gen():
        usage_tokens: int | None = None
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            obj = json.loads(data)
                            u = obj.get("usage") or {}
                            if u.get("total_tokens"):
                                usage_tokens = int(u["total_tokens"])
                        except Exception:
                            pass
                    yield line + "\n\n"
        except Exception:
            pass  # 客户端断连等异常：按已累积 usage 结算
        finally:
            await stream.__aexit__(None, None, None)
            await _close_upstream_client(client, provider)
            if usage_tokens is not None:
                await run_in_threadpool(settle, res.key_id, res.reservation_id, usage_tokens)
            # usage 缺失 → 保持预扣留存

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/v1/chat/fail")
async def chat_fail(request: Request, body: ChatBody):
    """演示 5xx 语义：预扣后服务端故障，全额退款。"""

    @require_key()
    async def _handler(req: Request):
        provider, model = await run_in_threadpool(
            _resolve_route, request.headers.get("X-Api-Key", ""), None, body.model
        )
        res = await reserve_for_request(req, reserve_tokens=body.max_tokens,
                                  provider=provider.id, model=model)
        if not res.ok:
            raise _quota_error(res)
        raise RuntimeError("上游模型 5xx（模拟）")

    await _handler(request)
    raise HTTPException(500, "unreachable")


@app.exception_handler(RuntimeError)
async def _runtime_error_handler(request: Request, exc: RuntimeError):
    # 服务端未捕获异常：全额退预扣（token + 金额 + 次数），不赖用户
    res = getattr(request.state, "reservation", None)
    if res is not None and res.ok:
        await run_in_threadpool(refund, res.reservation_id)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.exception_handler(httpx.HTTPError)
async def _upstream_error_handler(_: Request, exc: httpx.HTTPError):
    return JSONResponse(status_code=502, content={"detail": f"upstream: {exc.__class__.__name__}"})


# ---------- 页面 ----------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------- 启动 ----------

init_db()

from . import provider_store  # noqa: E402

provider_store.init_provider_store()
registry.reload()

from .security import admin_enabled  # noqa: E402

if not admin_enabled():
    print("[提示] 未设置环境变量 KEYTOOL_ADMIN_TOKEN，管理接口（签发/列表/吊销/充值/日志/热重载）无鉴权保护")
    print("       生产环境请设置 KEYTOOL_ADMIN_TOKEN 后启动")



