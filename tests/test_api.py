"""API 与额度核心测试：签发 / 消费 / 退差 / 一次性 / 重置 / 并发 / 5xx / 上游转发。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_conn


def _issue(client, **limits):
    body = {k: v for k, v in limits.items() if v is not None}
    r = client.post("/keys", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _chat(client, token, prompt="你好", max_tokens=256):
    return client.post(
        "/v1/chat",
        json={"prompt": prompt, "max_tokens": max_tokens},
        headers={"X-Api-Key": token},
    )


# ---------- 签发与查余量 ----------


def test_issue_and_status(client):
    data = _issue(client, calls=100, tokens=1_000_000, expires="30d", note="测试")
    assert data["key"].startswith("sk-")
    assert data["token_hash_prefix"]

    st = client.get(f"/keys/{data['id']}").json()
    assert st["status"] == "active"
    assert st["calls"] == {"used": 0, "total": 100}
    assert st["tokens"] == {"used": 0, "total": 1_000_000}
    assert st["expires_at"] is not None


def test_issue_no_limits_is_unlimited(client):
    data = _issue(client)
    st = client.get(f"/keys/{data['id']}").json()
    assert st["calls"]["total"] is None
    assert st["tokens"]["total"] is None


def test_issue_invalid(client):
    assert client.post("/keys", json={"calls": 0}).status_code == 422
    assert client.post("/keys", json={"expires": "xyz"}).status_code == 422


# ---------- 消费与结算 ----------


def test_consume_ok_and_settle_refund(client):
    data = _issue(client, tokens=1_000_000)
    token = data["key"]

    # 预扣 max_tokens=1024，mock 上游实际用 100 → 退差后 tokens_used = 100
    r = _chat(client, token, prompt="x" * 200, max_tokens=1024)
    assert r.status_code == 200
    assert r.json()["usage"]["total_tokens"] == 100
    assert r.json()["model"] == "fake-model"

    st = client.get(f"/keys/{data['id']}").json()
    assert st["calls"]["used"] == 1
    assert st["tokens"]["used"] == 100  # 预扣 1024 → 退 924


def test_once_use_key(client):
    data = _issue(client, calls=1)  # 一次性
    token = data["key"]

    assert _chat(client, token).status_code == 200
    r = _chat(client, token)  # 第二次
    assert r.status_code == 402
    assert r.json()["detail"] == "call quota exhausted"


def test_calls_exhausted(client):
    data = _issue(client, calls=2)
    token = data["key"]
    assert _chat(client, token).status_code == 200
    assert _chat(client, token).status_code == 200
    r = _chat(client, token)
    assert r.status_code == 402


def test_token_exhausted(client):
    data = _issue(client, tokens=100)
    token = data["key"]
    r = _chat(client, token, prompt="x" * 400, max_tokens=1024)  # 预扣 1024 > 100
    assert r.status_code == 402
    assert r.json()["detail"] == "token quota exhausted"


def test_missing_and_invalid_key(client):
    assert client.post("/v1/chat", json={"prompt": "hi"}).status_code == 401
    r = client.post(
        "/v1/chat", json={"prompt": "hi"}, headers={"X-Api-Key": "sk-fake"}
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid api key"


def test_expired_key(client):
    data = _issue(client, expires="1h")
    with get_conn() as conn:
        conn.execute(
            "UPDATE api_keys SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (data["id"],),
        )
    r = _chat(client, data["key"])
    assert r.status_code == 410


def test_revoked_key(client):
    data = _issue(client)
    assert client.post(f"/keys/{data['id']}/revoke").status_code == 200
    r = _chat(client, data["key"])
    assert r.status_code == 403
    assert r.json()["detail"] == "api key revoked"


def test_revoke_missing(client):
    assert client.post("/keys/nope/revoke").status_code == 404


# ---------- 5xx 全额退 ----------


def test_5xx_refunds_reserved_tokens(client):
    data = _issue(client, tokens=1_000_000)
    token = data["key"]
    r = client.post(
        "/v1/chat/fail",
        json={"prompt": "hi", "max_tokens": 4096},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 500
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0   # 预扣 4096 全额退回
    assert st["calls"]["used"] == 0    # 5xx 不计次数


# ---------- 懒重置 ----------


def test_monthly_lazy_reset(client):
    data = _issue(client, calls=5, cycle="monthly")
    token = data["key"]
    # 模拟：当月额度已用 4 次
    with get_conn() as conn:
        conn.execute(
            "UPDATE api_keys SET calls_used=4, quota_cycle='2000-01' WHERE id=?",
            (data["id"],),
        )
    # 跨月首次消费 → 清零再扣，等于 used=1
    assert _chat(client, token).status_code == 200
    st = client.get(f"/keys/{data['id']}").json()
    assert st["calls"]["used"] == 1
    assert st["quota_cycle"] != "2000-01"


def test_no_reset_without_cycle(client):
    data = _issue(client, calls=5)  # 无 cycle
    with get_conn() as conn:
        conn.execute("UPDATE api_keys SET calls_used=4 WHERE id=?", (data["id"],))
    assert _chat(client, token=data["key"]).status_code == 200
    assert _chat(client, token=data["key"]).status_code == 402  # 第 6 次超限


# ---------- 充值 ----------


def test_recharge(client):
    data = _issue(client, calls=1)
    r = client.post(f"/keys/{data['id']}/recharge", json={"calls": 10})
    assert r.status_code == 200
    st = client.get(f"/keys/{data['id']}").json()
    assert st["calls"]["total"] == 11


# ---------- 并发不超扣 ----------

def test_concurrent_no_overdraw(client):
    data = _issue(client, calls=5)
    token = data["key"]

    def call(_):
        return _chat(client, token).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(call, range(20)))

    assert codes.count(200) == 5
    assert codes.count(402) == 15
    st = client.get(f"/keys/{data['id']}").json()
    assert st["calls"]["used"] == 5  # 绝不超扣


# ---------- 限流 ----------


def test_rate_limit(client):
    from app.middleware import limiter

    data = _issue(client)
    token = data["key"]
    old = limiter.max_rps
    limiter.max_rps = 2
    try:
        codes = [_chat(client, token).status_code for _ in range(4)]
    finally:
        limiter.max_rps = old
    assert 429 in codes


# ---------- 上游转发 ----------


def test_upstream_5xx_502_and_refund(client, mock_upstream):
    mock_upstream(status=500)
    data = _issue(client, tokens=1_000_000)
    token = data["key"]
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 4096},
        headers={"X-Api-Key": token},
    )
    assert r.status_code == 502
    assert "500" in r.json()["detail"]
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0  # 全额退
    assert st["calls"]["used"] == 0   # 不计次数


def test_upstream_4xx_502_and_refund(client, mock_upstream):
    mock_upstream(status=400)
    data = _issue(client, tokens=1000)
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 512},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 502
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0
    assert st["calls"]["used"] == 0


def test_upstream_network_error_502_and_refund(client):
    import httpx

    from app.providers import Provider, registry

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=None)

    registry.set(
        [Provider(id="fake", name="Fake", base_url="https://fake/v1",
                  api_key="k", default_model="m",
                  transport=httpx.MockTransport(boom))],
        default_id="fake",
    )
    data = _issue(client, tokens=1000)
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 512},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 502
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0
    assert st["calls"]["used"] == 0


def test_no_provider_503(client):
    from app.providers import registry

    registry.set([])
    data = _issue(client)  # 不绑定 provider，允许签发
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 128},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 503
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0  # 未消费


def test_providers_endpoint(client):
    r = client.get("/providers")
    assert r.status_code == 200
    data = r.json()
    assert data["default"] == "fake"
    assert any(p["id"] == "fake" for p in data["providers"])
    assert "api_key" not in str(data)  # 不暴露密钥
    # 预设模型列表
    fake = next(p for p in data["providers"] if p["id"] == "fake")
    assert "fake-model" in fake["models"]
    assert "fake-model-2" in fake["models"]
    assert fake["default_model"] == "fake-model"


def test_issue_unknown_provider_422(client):
    r = client.post("/keys", json={"provider": "nope"})
    assert r.status_code == 422


# ---------- key 绑定模型/服务商 ----------


def test_bind_model_ok(client):
    data = _issue(client, calls=5, provider="fake", model="fake-model")
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 128, "model": "fake-model"},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 200


def test_bind_model_forbidden(client):
    data = _issue(client, calls=5, provider="fake", model="fake-model")
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 128, "model": "gpt-4o"},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 403
    assert "model not allowed" in r.json()["detail"]


def test_bind_provider_forbidden(client):
    data = _issue(client, calls=5, provider="fake", model="fake-model")
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 128, "provider": "openai"},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 403
    assert "provider not allowed" in r.json()["detail"]


def test_bound_key_status_shows_binding(client):
    data = _issue(client, provider="fake", model="fake-model")
    st = client.get(f"/keys/{data['id']}").json()
    assert st["provider"] == "fake"
    assert st["model"] == "fake-model"


# ---------- 管理接口 ----------


def test_list_keys_and_logs(client):
    d1 = _issue(client, calls=3)
    _chat(client, d1["key"])
    d2 = _issue(client, calls=1)

    r = client.get("/keys")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    ids = {k["id"] for k in data["items"]}
    assert ids == {d1["id"], d2["id"]}
    # 列表不含 token_hash
    assert "token_hash" not in str(data)

    # 状态筛选
    client.post(f"/keys/{d1['id']}/revoke")
    r = client.get("/keys?status=revoked")
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == d1["id"]

    # 审计日志（d1 消费过，d2 未消费）
    r = client.get(f"/keys/{d1['id']}/logs")
    assert r.status_code == 200
    assert r.json()["total"] >= 1
    actions = {l["action"] for l in r.json()["items"]}
    # 预扣记录保留（settle_reserved）+ 结算记录（settle）都在审计里
    assert "settle" in actions and "settle_reserved" in actions
    # d2 未消费 → 无日志
    assert client.get(f"/keys/{d2['id']}/logs").json()["total"] == 0

    # 未知 key
    assert client.get("/keys/nope/logs").status_code == 404


# ---------- ADMIN_TOKEN ----------


def test_admin_token_protection(client, monkeypatch):
    monkeypatch.setenv("KEYTOOL_ADMIN_TOKEN", "secret123")
    # 未带令牌 → 401
    assert client.get("/keys").status_code == 401
    assert client.post("/keys", json={"calls": 1}).status_code == 401
    assert client.post("/providers/reload").status_code == 401
    # 错误令牌 → 401
    assert client.get("/keys", headers={"X-Admin-Token": "wrong"}).status_code == 401
    # 正确令牌（X-Admin-Token 与 Bearer 均可）→ 放行
    assert client.get("/keys", headers={"X-Admin-Token": "secret123"}).status_code == 200
    assert client.get("/keys", headers={"Authorization": "Bearer secret123"}).status_code == 200


def test_admin_disabled_no_token_needed(client):
    # 未配置 KEYTOOL_ADMIN_TOKEN 时管理接口免鉴权
    assert client.get("/keys").status_code == 200
    assert client.post("/keys", json={"calls": 1}).status_code == 201


def test_admin_login_cookie(client, monkeypatch):
    monkeypatch.setenv("KEYTOOL_ADMIN_TOKEN", "secret123")
    # 错误令牌登录 → 401
    assert client.post("/admin/login", json={"token": "wrong"}).status_code == 401

    # 正确登录 → 下发 cookie
    r = client.post("/admin/login", json={"token": "secret123"})
    assert r.status_code == 200
    cookie = r.cookies.get("keytool_admin")
    assert cookie and "." in cookie  # 签名 cookie

    # 携带 cookie 访问管理接口 → 放行
    client.cookies.set("keytool_admin", cookie)
    assert client.get("/keys").status_code == 200

    # 伪造 cookie → 401
    client.cookies.set("keytool_admin", "9999999999.deadbeef")
    assert client.get("/keys").status_code == 401

    # 登出后 cookie 失效（新请求无 cookie）→ 401
    client.cookies.clear()
    client.post("/admin/login", json={"token": "secret123"})
    client.post("/admin/logout")
    assert client.get("/keys").status_code == 401


def test_admin_me(client, monkeypatch):
    monkeypatch.setenv("KEYTOOL_ADMIN_TOKEN", "secret123")
    # 未登录
    assert client.get("/admin/me").json() == {"admin": False}
    # 登录后
    client.post("/admin/login", json={"token": "secret123"})
    assert client.get("/admin/me").json() == {"admin": True}
    # 未配置令牌时始终 admin=True
    monkeypatch.delenv("KEYTOOL_ADMIN_TOKEN")
    assert client.get("/admin/me").json() == {"admin": True}


# ---------- 流式转发 ----------


def test_stream_chat(client):
    import json as _json

    import httpx

    from app.providers import Provider, registry

    def sse_handler(_: httpx.Request) -> httpx.Response:
        text = (
            'data: {"id":"x","choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"id":"x","choices":[{"delta":{"content":"世界"}}]}\n\n'
            'data: {"id":"x","choices":[],"usage":{"total_tokens":42}}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

    registry.set(
        [Provider(id="fake", name="Fake", base_url="https://fake/v1",
                  api_key="k", default_model="m",
                  transport=httpx.MockTransport(sse_handler))],
        default_id="fake",
    )
    data = _issue(client, tokens=1_000_000)
    with client.stream(
        "POST", "/v1/chat",
        json={"prompt": "hi", "max_tokens": 1024, "stream": True},
        headers={"X-Api-Key": data["key"]},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())

    assert 'delta":{"content":"你好"}' in body
    assert 'usage":{"total_tokens":42}' in body
    assert "data: [DONE]" in body

    # 结束时按 usage=42 退差（预扣 1024 → 结算 42）
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 42
    assert st["calls"]["used"] == 1


def test_stream_upstream_error_refunds(client):
    import httpx

    from app.providers import Provider, registry

    def err_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "upstream down"}})

    registry.set(
        [Provider(id="fake", name="Fake", base_url="https://fake/v1",
                  api_key="k", default_model="m",
                  transport=httpx.MockTransport(err_handler))],
        default_id="fake",
    )
    data = _issue(client, tokens=1000)
    r = client.post(
        "/v1/chat",
        json={"prompt": "hi", "max_tokens": 512, "stream": True},
        headers={"X-Api-Key": data["key"]},
    )
    assert r.status_code == 502
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 0
    assert st["calls"]["used"] == 0


# ---------- 服务商热重载 ----------


def test_provider_crud(client):
    # 创建（DB 存储 + 加密）
    r = client.post("/providers", json={
        "id": "px", "name": "PX", "base_url": "https://px/v1",
        "api_key": "sk-secret", "default_model": "m1",
        "models": ["m1", "m2"], "is_default": True,
    })
    assert r.status_code == 201
    assert r.json()["models"] == ["m1", "m2"]
    assert r.json()["encrypted"] in (True, False)

    # 列表（registry 已重载）
    data = client.get("/providers").json()
    px = next(p for p in data["providers"] if p["id"] == "px")
    assert px["models"] == ["m1", "m2"]
    assert data["default"] == "px"
    assert "api_key" not in str(data)  # 不泄露密钥

    # reload 接口从 DB 重载
    r = client.post("/providers/reload")
    assert r.json()["reloaded"] >= 1

    # 更新
    r = client.put("/providers/px", json={
        "id": "px", "name": "PX2", "base_url": "https://px/v1",
        "api_key": "sk-new", "default_model": "m2", "models": ["m1", "m2"],
    })
    assert r.status_code == 200
    data = client.get("/providers").json()
    assert next(p for p in data["providers"] if p["id"] == "px")["name"] == "PX2"

    # 删除
    assert client.delete("/providers/px").status_code == 200
    data = client.get("/providers").json()
    assert all(p["id"] != "px" for p in data["providers"])
    assert client.delete("/providers/px").status_code == 404


def test_provider_encryption(client, monkeypatch):
    """配置 KEYTOOL_SECRET_KEY 后 api_key 加密存储。"""
    import base64

    from cryptography.fernet import Fernet

    monkeypatch.setenv("KEYTOOL_SECRET_KEY",
                       base64.urlsafe_b64encode(b"0" * 32).decode())
    from app import provider_store

    assert provider_store.encryption_enabled()
    r = client.post("/providers", json={
        "id": "penc", "name": "PEnc", "base_url": "https://p/v1",
        "api_key": "sk-top-secret", "default_model": "m",
    })
    assert r.status_code == 201
    # DB 里是密文
    from app.db import get_conn as _gc

    with _gc() as conn:
        stored = conn.execute(
            "SELECT api_key_enc FROM providers WHERE id='penc'"
        ).fetchone()["api_key_enc"]
    assert stored.startswith("enc:")
    assert "sk-top-secret" not in stored
    # 读回是明文
    p = provider_store.list_all()
    assert any(x.id == "penc" and x.api_key == "sk-top-secret" for x in p)
    # 清理
    client.delete("/providers/penc")


# ---------- 预设服务商 + 填 key ----------


def test_presets_seeded_and_env_ref(client):
    """内置预设已 seed；${ENV} 引用在环境变量未设置时 needs_key=True。"""
    from app import provider_store
    from app.providers import registry

    registry.reload()  # 从 DB 加载预设（测试默认注入的是 fake）
    try:
        presets = provider_store.list_all()
        ids = {p.id for p in presets}
        # 六家预设至少包含 DeepSeek / OpenAI / Ollama
        assert {"deepseek", "openai", "ollama"} <= ids
        ds = next(p for p in presets if p.id == "deepseek")
        assert ds.base_url == "https://api.deepseek.com/v1"
        assert "deepseek-chat" in ds.models
        # 未设置 DEEPSEEK_API_KEY → api_key 为空 → needs_key
        data = client.get("/providers").json()
        d = next(p for p in data["providers"] if p["id"] == "deepseek")
        assert d["needs_key"] is True
    finally:
        registry.reload()  # 恢复测试默认（autouse 快照会再次兜底）


def test_presets_env_resolved(client, monkeypatch):
    """设置环境变量后预设 api_key 被解析，needs_key=False。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-key")
    from app.providers import registry

    registry.reload()  # 重新解析环境变量
    try:
        data = client.get("/providers").json()
        d = next(p for p in data["providers"] if p["id"] == "deepseek")
        assert d["needs_key"] is False
    finally:
        registry.reload()  # 环境变量已由 monkeypatch 管理，autouse 快照兜底


def test_provider_update_key(client):
    from app import provider_store

    # 手动添加一个无 key 标记的场景：更新 key
    r = client.post("/providers", json={
        "id": "pkey", "name": "PKey", "base_url": "https://p/v1",
        "api_key": "${PKEY_ENV}", "default_model": "m",
    })
    assert r.status_code == 201
    # 未设置环境变量 → needs_key
    data = client.get("/providers").json()
    assert next(p for p in data["providers"] if p["id"] == "pkey")["needs_key"] is True

    # 填 key
    r = client.post("/providers/pkey/key", json={"api_key": "sk-typed-key"})
    assert r.status_code == 200
    data = client.get("/providers").json()
    assert next(p for p in data["providers"] if p["id"] == "pkey")["needs_key"] is False
    # 读回明文
    p = provider_store.list_all()
    assert next(x for x in p if x.id == "pkey").api_key == "sk-typed-key"

    # 未知服务商 → 404
    assert client.post("/providers/nope/key", json={"api_key": "x"}).status_code == 404
    client.delete("/providers/pkey")


def test_providers_presets_endpoint(client):
    """一键添加预设接口（幂等，已存在的不重复加）。"""
    from app import provider_store
    from app.db import get_conn

    # 先删掉 deepseek 再一键添加，验证能补回
    with get_conn() as conn:
        conn.execute("DELETE FROM providers WHERE id='deepseek'")
    r = client.post("/providers/presets")
    assert r.status_code == 200
    assert r.json()["added"] >= 1
    assert any(p.id == "deepseek" for p in provider_store.list_all())


# ---------- 项目体系 ----------


def _mk_project(client, **kw):
    body = {"name": kw.pop("name", "测试项目")}
    body.update(kw)
    r = client.post("/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_project_crud_and_recharge(client):
    p = _mk_project(client, calls=100, tokens=1_000_000, balance=10.0, owner="me")
    pid = p["id"]
    assert p["balance"] == 10.0

    lst = client.get("/projects").json()["items"]
    assert any(x["id"] == pid for x in lst)

    st = client.get(f"/projects/{pid}").json()
    assert st["calls"] == {"used": 0, "total": 100}

    # 充值
    r = client.post(f"/projects/{pid}/recharge", json={"calls": 50, "balance": 5})
    assert r.status_code == 200
    st = client.get(f"/projects/{pid}").json()
    assert st["calls"]["total"] == 150
    assert st["balance"] == 15.0

    assert client.get("/projects/nope").status_code == 404


def test_project_shared_pool(client):
    # 项目池 3 次，两个 key 共享
    p = _mk_project(client, calls=3)
    k1 = _issue(client, project_id=p["id"])
    k2 = _issue(client, project_id=p["id"])

    assert _chat(client, k1["key"]).status_code == 200
    assert _chat(client, k1["key"]).status_code == 200
    assert _chat(client, k2["key"]).status_code == 200
    # 池用尽
    r = _chat(client, k1["key"])
    assert r.status_code == 402
    assert r.json()["detail"] == "project call quota exhausted"

    st = client.get(f"/projects/{p['id']}").json()
    assert st["calls"] == {"used": 3, "total": 3}


def test_key_quota_and_project_pool_stack(client):
    # key 独立 2 次 + 项目池 5 次：key 先耗尽
    p = _mk_project(client, calls=5)
    k = _issue(client, project_id=p["id"], calls=2)
    assert _chat(client, k["key"]).status_code == 200
    assert _chat(client, k["key"]).status_code == 200
    r = _chat(client, k["key"])
    assert r.status_code == 402
    assert r.json()["detail"] == "call quota exhausted"  # key 层
    st = client.get(f"/projects/{p['id']}").json()
    assert st["calls"]["used"] == 2  # 池只被扣了 2 次（第 3 次 key 层拒绝，整体回滚）


def test_issue_unknown_project_422(client):
    r = client.post("/keys", json={"project_id": "nope"})
    assert r.status_code == 422


# ---------- 余额计费 ----------


def test_balance_billing(client):
    # 定价：100 元 / 1M tokens
    r = client.post("/pricing", json={"provider": "fake", "model": "fake-model",
                                      "price_per_million": 100})
    assert r.status_code == 201
    p = _mk_project(client, balance=1.0)
    k = _issue(client, project_id=p["id"], tokens=1_000_000)

    # 预扣 max_tokens=1024 → 0.1024 元；实际 usage=100 → 0.01 元，退差 0.0924
    assert _chat(client, k["key"], prompt="hi", max_tokens=1024).status_code == 200
    st = client.get(f"/projects/{p['id']}").json()
    assert st["balance"] == pytest.approx(1.0 - 0.1024 + 0.0924, abs=1e-6)
    # 项目 token 池按实际扣
    assert st["tokens"]["used"] == 100


def test_balance_insufficient(client):
    client.post("/pricing", json={"provider": "fake", "model": "fake-model",
                                  "price_per_million": 100})
    p = _mk_project(client, balance=0.05)  # 0.05 元 < 预扣 0.1024 元
    k = _issue(client, project_id=p["id"], tokens=1_000_000)
    r = _chat(client, k["key"], max_tokens=1024)
    assert r.status_code == 402
    assert r.json()["detail"] == "project balance insufficient"
    st = client.get(f"/projects/{p['id']}").json()
    assert st["balance"] == pytest.approx(0.05, abs=1e-9)  # 未扣


def test_no_balance_no_billing(client):
    # 项目无余额（balance NULL）→ 不计费
    p = _mk_project(client, tokens=1_000_000)
    k = _issue(client, project_id=p["id"])
    assert _chat(client, k["key"], max_tokens=1024).status_code == 200
    st = client.get(f"/projects/{p['id']}").json()
    assert st["balance"] is None


# ---------- 按 key 限流 ----------


def test_rps_limit_per_key(client):
    k = _issue(client, rps_limit=2)
    codes = [_chat(client, k["key"]).status_code for _ in range(4)]
    assert 429 in codes
    # 未配 rps 的 key 用全局默认，不受影响
    k2 = _issue(client)
    assert _chat(client, k2["key"]).status_code == 200


# ---------- 吊销重签 ----------


def test_rotate(client):
    k = _issue(client, calls=5, note="abc")
    # 用掉 2 次
    assert _chat(client, k["key"]).status_code == 200
    assert _chat(client, k["key"]).status_code == 200

    r = client.post(f"/keys/{k['id']}/rotate")
    assert r.status_code == 200
    new_key = r.json()["key"]
    assert new_key != k["key"]
    assert r.json()["limits"]["calls"] == 5
    assert r.json()["limits"]["note"].startswith("abc")

    # 旧 key 已吊销 → 403（存在但已吊销）
    assert _chat(client, k["key"]).status_code == 403
    # 新 key 剩余额度 = 5 - 2 = 3
    for _ in range(3):
        assert _chat(client, new_key).status_code == 200
    assert _chat(client, new_key).status_code == 402

    # 重复 rotate → 404
    assert client.post(f"/keys/{k['id']}/rotate").status_code == 404


# ---------- 用量报表 ----------


def test_stats_usage(client):
    p = _mk_project(client, calls=1000, tokens=1_000_000, balance=10.0)
    client.post("/pricing", json={"provider": "fake", "model": "fake-model",
                                  "price_per_million": 100})
    k = _issue(client, project_id=p["id"])
    assert _chat(client, k["key"], max_tokens=1024).status_code == 200
    assert _chat(client, k["key"], max_tokens=1024).status_code == 200

    r = client.get(f"/stats/usage?project_id={p['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["total"]["calls"] == 2
    assert data["total"]["tokens"] == 200  # 2 × usage=100
    assert data["total"]["cost"] == pytest.approx(0.02, abs=1e-6)  # 2 × 100tok × 100元/1M
    assert len(data["days"]) >= 1

    # 全量累计
    assert data["project_totals"]["calls"] == 2
    # 无项目维度
    r2 = client.get("/stats/usage")
    assert r2.json()["total"]["calls"] == 2


# ---------- Redis 限流后端 ----------


def test_redis_limiter():
    import fakeredis

    from app.redis_limiter import RedisSlidingWindowLimiter

    client = fakeredis.FakeRedis(decode_responses=True)
    limiter = RedisSlidingWindowLimiter(client, max_rps=2)
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is True
    assert limiter.allow("k1") is False   # 超 2 rps
    assert limiter.allow("k2") is True    # 不同 key 独立
    limiter.reset("k1")
    assert limiter.allow("k1") is True


# ---------- OpenAI 兼容端点 ----------

def test_openai_completions(client):
    data = _issue(client, tokens=1_000_000)
    # Bearer 鉴权 + OpenAI 请求格式（messages 数组）
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ], "max_tokens": 1024, "temperature": 0.7},
        headers={"Authorization": f"Bearer {data['key']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "fake-model"
    assert body["choices"][0]["message"]["content"] == "mock reply"
    assert body["usage"]["total_tokens"] == 100
    # 按 usage 结算
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 100
    # 小写 bearer 同样放行
    r2 = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"bearer {data['key']}"},
    )
    assert r2.status_code == 200


def test_openai_completions_stream(client):
    import httpx

    from app.providers import Provider, registry

    def sse_handler(_: httpx.Request) -> httpx.Response:
        text = (
            'data: {"id":"x","object":"chat.completion.chunk","choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"id":"x","object":"chat.completion.chunk","choices":[],"usage":{"total_tokens":42}}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

    registry.set(
        [Provider(id="fake", name="Fake", base_url="https://fake/v1",
                  api_key="k", default_model="m",
                  transport=httpx.MockTransport(sse_handler))],
        default_id="fake",
    )
    data = _issue(client, tokens=1_000_000)
    with client.stream(
        "POST", "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": f"Bearer {data['key']}"},
    ) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert 'object":"chat.completion.chunk"' in body
    assert "data: [DONE]" in body
    st = client.get(f"/keys/{data['id']}").json()
    assert st["tokens"]["used"] == 42


def test_openai_completions_bearer_required(client):
    # 无鉴权 → 401
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_openai_completions_key_model_binding(client):
    # key 绑定 model，请求指定其他 model → 403
    data = _issue(client, provider="fake", model="fake-model")
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {data['key']}"},
    )
    assert r.status_code == 403

# ---------- CLI ----------


def _cli(url, *argv):
    """直接运行 CLI（真实连 live_server），返回 (code, stdout)。"""
    import io
    import contextlib

    import keytool

    keytool.DEFAULT_URL = url
    buf = io.StringIO()
    code = 0
    try:
        args = keytool.build_parser().parse_args(list(argv))
        with contextlib.redirect_stdout(buf):
            args.func(args)
    except SystemExit as e:
        code = int(e.code or 0)
    return code, buf.getvalue()


def test_cli_issue_list_chat(client, live_server):
    code, out = _cli(live_server, "issue", "--calls", "5", "--note", "cli-test")
    assert code == 0
    assert "Key:" in out
    key = out.split("Key:")[1].split()[0].strip()
    kid = out.split("ID:")[1].split()[0].strip()
    assert key.startswith("sk-")

    code, out = _cli(live_server, "list")
    assert code == 0
    assert kid in out  # 列表按公开 ID 显示

    code, out = _cli(live_server, "chat", key, "你好")
    assert code == 0
    assert "usage" in out

    code, out = _cli(live_server, "status", kid)
    assert code == 0
    import json as _json

    st = _json.loads(out)
    assert st["calls"]["used"] == 1


def test_cli_project_revoke(client, live_server):
    code, out = _cli(live_server, "project", "--name", "cli项目", "--balance", "5")
    assert code == 0
    pid = out.split(":")[1].split()[0].strip()

    code, out = _cli(live_server, "issue", "--project", pid)
    assert code == 0
    key = out.split("Key:")[1].split()[0].strip()
    kid = out.split("ID:")[1].split()[0].strip()

    code, out = _cli(live_server, "projects")
    assert code == 0
    assert "cli项目" in out

    code, out = _cli(live_server, "revoke", kid)
    assert code == 0
    assert "已吊销" in out

# ---------- 审查修复回归 ----------


def test_settle_caps_overage_balance_not_negative(client):
    """结算时实际用量超预扣量 → 按预扣量封顶，余额不为负，记 warn 审计。"""
    client.post("/pricing", json={"provider": "fake", "model": "fake-model",
                                  "price_per_million": 100})
    p = _mk_project(client, balance=0.02)
    k = _issue(client, project_id=p["id"], tokens=1000)
    # max_tokens=10 → 预扣 10；mock 上游 usage=100（超预扣）
    r = _chat(client, k["key"], max_tokens=10)
    assert r.status_code == 200
    st = client.get(f"/projects/{p['id']}").json()
    assert st["tokens"]["used"] == 10          # 封顶
    assert st["balance"] >= 0                   # 不为负
    assert st["balance"] == pytest.approx(0.02 - 10 * 100 / 1_000_000, abs=1e-6)
    logs = client.get(f"/keys/{k['id']}/logs").json()["items"]
    assert any(l["action"] == "warn" for l in logs)


def test_double_settle_idempotent(client):
    """同一 reservation 重复 settle → 仅首次生效。"""
    from app.quota import reserve, settle

    k = _issue(client, tokens=1000)
    res = reserve(k["key"], reserve_tokens=100, provider="fake", model="fake-model")
    assert res.ok
    assert settle(res.key_id, res.reservation_id, 50) is True
    assert settle(res.key_id, res.reservation_id, 50) is False  # 幂等
    st = client.get(f"/keys/{k['id']}").json()
    assert st["tokens"]["used"] == 50


def test_double_refund_idempotent(client):
    from app.quota import refund, reserve

    k = _issue(client, tokens=1000)
    res = reserve(k["key"], reserve_tokens=100, provider="fake", model="fake-model")
    assert res.ok
    assert refund(res.reservation_id) is True
    assert refund(res.reservation_id) is False  # 幂等
    st = client.get(f"/keys/{k['id']}").json()
    assert st["tokens"]["used"] == 0


def test_rotate_preserves_expiry(client):
    k = _issue(client, calls=5, expires="30d")
    r = client.post(f"/keys/{k['id']}/rotate")
    assert r.status_code == 200
    assert r.json()["limits"]["expires_at"] is not None  # 剩余有效期保留


def test_rate_limit_uses_xff(client):
    from app.security import issue_limiter

    old = issue_limiter.max_rps
    issue_limiter.max_rps = 1
    try:
        r1 = client.post("/keys", json={"calls": 1}, headers={"X-Forwarded-For": "1.2.3.4"})
        r2 = client.post("/keys", json={"calls": 1}, headers={"X-Forwarded-For": "1.2.3.4"})
        assert r1.status_code == 201
        assert r2.status_code == 429
        r3 = client.post("/keys", json={"calls": 1}, headers={"X-Forwarded-For": "5.6.7.8"})
        assert r3.status_code == 201  # 不同 IP 独立计数
    finally:
        issue_limiter.max_rps = old


def test_provider_deleted_503_message(client):
    """key 绑定的服务商被删除后消费 → 503 且文案区分。"""
    client.post("/providers", json={
        "id": "px", "name": "PX", "base_url": "https://px/v1",
        "api_key": "sk-x", "default_model": "m",
    })
    k = _issue(client, provider="px", model="m")
    client.delete("/providers/px")
    r = _chat(client, k["key"])
    assert r.status_code == 503
    assert "已被删除" in r.json()["detail"]

# ---------- 并发/安全修复回归 ----------


def test_admin_login_rate_limited(client, monkeypatch):
    """/admin/login 按 IP 限流，防暴力猜测。"""
    from app.security import login_limiter

    monkeypatch.setenv("KEYTOOL_ADMIN_TOKEN", "secret123")
    login_limiter.max_rps = 3
    try:
        codes = []
        for _ in range(5):
            r = client.post("/admin/login", json={"token": "wrong"})
            codes.append(r.status_code)
        assert codes.count(401) >= 2
        assert 429 in codes  # 触发限流
    finally:
        login_limiter.max_rps = 5


def test_login_cookie_secure_flag(client, monkeypatch):
    """KEYTOOL_FORCE_SECURE_COOKIE=1 时会话 cookie 带 Secure 标志。"""
    import os

    monkeypatch.setenv("KEYTOOL_ADMIN_TOKEN", "secret123")
    monkeypatch.setenv("KEYTOOL_FORCE_SECURE_COOKIE", "1")
    r = client.post("/admin/login", json={"token": "secret123"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
