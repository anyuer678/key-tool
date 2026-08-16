"""消费侧：key 校验 + 按 key 限流 + 预留/结算编排。

v0.4 起 require_key 只做"身份校验 + 限流"，不再预扣——预扣（reserve）由
业务侧在确定 provider/model（计费需要价格）后显式调用，失败映射为
HTTP 语义，成功挂到 request.state.reservation，由业务 settle/refund 收尾。

HTTP 语义：
    401 无效 key / 缺失      403 吊销或项目不可用      410 已过期
    402 次数或 token 或余额耗尽    429 限流
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .db import get_conn
from .keys import sha256, status
from .quota import ReserveResult, refund, reserve, settle

DEFAULT_RPS = 20

# ---- 内存滑动窗口限流（单进程；多进程换 Redis 后端见 app/redis_limiter.py）----


class SlidingWindowLimiter:
    def __init__(self, max_rps: int = DEFAULT_RPS):
        self.max_rps = max_rps
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, max_rps: int | None = None) -> bool:
        limit = max_rps or self.max_rps
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 1.0:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        """清空计数（测试隔离用）。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


def _make_limiter():
    """REDIS_URL 配置时用 Redis 共享限流，否则内存限流。"""
    from .redis_limiter import RedisSlidingWindowLimiter, redis_client_from_env

    client = redis_client_from_env()
    if client is not None:
        return RedisSlidingWindowLimiter(client)
    return SlidingWindowLimiter()


limiter = _make_limiter()


def _quota_error(res: ReserveResult) -> HTTPException:
    mapping = {
        "invalid_key": (401, "invalid api key"),
        "revoked": (403, "api key revoked"),
        "expired": (410, "api key expired"),
        "out_of_calls": (402, "call quota exhausted"),
        "out_of_tokens": (402, "token quota exhausted"),
        "project_not_found": (403, "project not found"),
        "project_archived": (403, "project archived"),
        "project_out_of_calls": (402, "project call quota exhausted"),
        "project_out_of_tokens": (402, "project token quota exhausted"),
        "project_insufficient_balance": (402, "project balance insufficient"),
    }
    code, msg = mapping.get(res.reason, (402, "quota exceeded"))
    return HTTPException(status_code=code, detail=msg)


def require_key() -> Callable:
    """保护业务接口：校验 key 存在 + 按 key 的 rps_limit 限流。

    校验通过后 request.state.key_id / request.state.token 可用；
    预扣由业务侧调用 reserve() 完成。
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            token = request.headers.get("X-Api-Key", "")
            if not token:
                # OpenAI 兼容客户端用 Authorization: Bearer（大小写不敏感）
                auth = request.headers.get("Authorization", "")
                if auth[:7].lower() == "bearer ":
                    token = auth[7:]
            if not token:
                raise HTTPException(401, "missing api key")

            async def _lookup() -> tuple[str, int | None] | None:
                def _q():
                    with get_conn() as conn:
                        row = conn.execute(
                            "SELECT id, rps_limit FROM api_keys WHERE token_hash = ?",
                            (sha256(token),),
                        ).fetchone()
                    return (row["id"], row["rps_limit"]) if row else None
                return await run_in_threadpool(_q)

            row = await _lookup()
            if row is None:
                raise HTTPException(401, "invalid api key")

            if not limiter.allow(token, row[1] or None):
                raise HTTPException(
                    status_code=429,
                    detail="rate limited",
                    headers={"Retry-After": "1"},
                )

            request.state.key_id = row[0]
            request.state.token = token
            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


async def reserve_for_request(request: Request, reserve_tokens: int = 0,
                              provider: str | None = None,
                              model: str | None = None) -> ReserveResult:
    """业务侧预扣（async 版，DB 调用移线程池避免阻塞事件循环）。"""
    token: str = request.state.token
    res: ReserveResult = await run_in_threadpool(
        reserve, token, reserve_tokens, 1, provider, model
    )
    if res.ok:
        request.state.reservation = res
    return res


def remaining_headers(token: str) -> dict[str, str]:
    """给响应附余量头（客户端自查）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE token_hash = ?", (sha256(token),)
        ).fetchone()
        key_id = row["id"] if row else None
    if key_id is None:
        return {}
    st = status(key_id)
    if st is None:
        return {}
    remaining_calls = (
        None
        if st["calls"]["total"] is None
        else st["calls"]["total"] - st["calls"]["used"]
    )
    remaining_tokens = (
        None
        if st["tokens"]["total"] is None
        else st["tokens"]["total"] - st["tokens"]["used"]
    )
    headers = {}
    if remaining_calls is not None:
        headers["X-RateLimit-Remaining-Calls"] = str(remaining_calls)
    if remaining_tokens is not None:
        headers["X-RateLimit-Remaining-Tokens"] = str(remaining_tokens)
    return headers

