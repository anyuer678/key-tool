"""管理面安全：ADMIN_TOKEN 保护 + 签发限流 + 签名会话 Cookie。

- ADMIN_TOKEN 从环境变量 KEYTOOL_ADMIN_TOKEN 读取；未配置时管理接口
  放行（本地单机模式），启动时打印警告。
- 配置后，管理接口要求以下任一：
  * `Authorization: Bearer <token>` 或 `X-Admin-Token` 头；
  * 登录接口颁发的 HttpOnly 签名会话 cookie（JS 不可读，防 XSS 窃取）。
- 签发接口按来源 IP 限流，防批量刷 key。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

from .middleware import SlidingWindowLimiter

ENV_TOKEN = "KEYTOOL_ADMIN_TOKEN"
COOKIE_NAME = "keytool_admin"
SESSION_TTL = 12 * 3600  # 会话有效期 12 小时


def admin_token() -> str | None:
    return os.environ.get(ENV_TOKEN) or None


def admin_enabled() -> bool:
    return admin_token() is not None


def issue_session_cookie(token: str) -> str:
    """登录成功：生成无状态签名 cookie（expires.hmac，重启不失效）。"""
    expires = int(time.time()) + SESSION_TTL
    payload = str(expires)
    sig = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_cookie(cookie: str, token: str) -> bool:
    """校验签名 cookie：签名匹配且未过期。"""
    try:
        expires_str, sig = cookie.rsplit(".", 1)
        expires = int(expires_str)
    except (ValueError, AttributeError):
        return False
    if expires < time.time():
        return False
    expected = hmac.new(token.encode("utf-8"), expires_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def require_admin(request: Request) -> None:
    """FastAPI 依赖：校验管理身份（头或会话 cookie；未配置则放行）。"""
    token = admin_token()
    if token is None:
        return  # 本地模式，未启用管理鉴权

    provided = request.headers.get("Authorization", "")
    if provided[:7].lower() == "bearer ":
        provided = provided[7:]
    if not provided:
        provided = request.headers.get("X-Admin-Token", "")
    if provided and hmac.compare_digest(provided, token):
        return

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and verify_session_cookie(cookie, token):
        return

    raise HTTPException(401, "invalid admin token")


# 签发限流：按来源 IP，默认 10 次/秒
issue_limiter = SlidingWindowLimiter(max_rps=10)


def _client_ip(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 首段（受信反代下），回退直连地址。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# 登录限流：按来源 IP，默认 5 次/秒（防 ADMIN_TOKEN 暴力猜测）
login_limiter = SlidingWindowLimiter(max_rps=5)


def rate_limit_login(request: Request) -> None:
    """FastAPI 依赖：/admin/login 按 IP 限流。"""
    ip = _client_ip(request)
    if not login_limiter.allow(ip):
        raise HTTPException(429, "too many login attempts", headers={"Retry-After": "1"})


def rate_limit_issue(request: Request) -> None:
    """FastAPI 依赖：签发接口按来源 IP 限流。"""
    ip = _client_ip(request)
    if not issue_limiter.allow(ip):
        raise HTTPException(429, "too many key issue requests", headers={"Retry-After": "1"})
