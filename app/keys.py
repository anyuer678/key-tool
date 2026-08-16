"""签发 / 吊销 / 充值 / 查余量。

密钥安全约定：
- 明文 token 只在签发时返回一次，服务端只存 SHA-256 哈希；
- 所有对外接口（查余量/吊销）使用公开 id，不暴露哈希；
- 日志中只记录 id 与哈希前缀，不记明文。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import get_conn

HASH_PREFIX_LEN = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_cycle() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_duration(text: str) -> timedelta:
    """解析 '30d' / '12h' / '45m' / '7d12h' 这类时长串。"""
    import re

    total = timedelta()
    for num, unit in re.findall(r"(\d+)\s*([dhms])", text.lower()):
        n = int(num)
        if unit == "d":
            total += timedelta(days=n)
        elif unit == "h":
            total += timedelta(hours=n)
        elif unit == "m":
            total += timedelta(minutes=n)
        else:
            total += timedelta(seconds=n)
    if total == timedelta():
        raise ValueError(f"无法解析时长: {text!r}")
    return total


@dataclass
class KeyLimits:
    """签发参数：三个维度独立可选，留空即不限。"""

    calls: int | None = None
    tokens: int | None = None
    expires: str | None = None      # 如 '30d'
    cycle: str | None = None        # 如 'monthly'
    provider: str | None = None     # 绑定服务商 id（NULL = 不限）
    model: str | None = None        # 绑定模型名（NULL = 不限）
    project_id: str | None = None   # 归属项目（NULL = 无项目）
    rps_limit: int | None = None    # 按 key 限流（NULL = 继承全局）
    note: str | None = None

    def validate(self) -> None:
        if self.calls is not None and self.calls < 1:
            raise ValueError("calls 至少为 1")
        if self.tokens is not None and self.tokens < 1:
            raise ValueError("tokens 至少为 1")
        if self.rps_limit is not None and self.rps_limit < 1:
            raise ValueError("rps_limit 至少为 1")
        if self.expires is not None:
            _parse_duration(self.expires)
        if self.cycle not in (None, "monthly", "daily", "hourly"):
            raise ValueError(f"不支持的 cycle: {self.cycle!r}")
        if self.provider is not None:
            from .providers import registry

            if registry.get(self.provider) is None:
                raise ValueError(f"未配置的服务商: {self.provider!r}（见 providers.json）")
        if self.project_id is not None:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM projects WHERE id = ?", (self.project_id,)
                ).fetchone()
            if row is None:
                raise ValueError(f"项目不存在: {self.project_id!r}")
            if row["status"] != "active":
                raise ValueError(f"项目已归档: {self.project_id!r}")


@dataclass
class IssuedKey:
    id: str
    token: str              # 明文，仅此一次
    token_hash_prefix: str
    limits: dict[str, Any]


def issue(limits: KeyLimits) -> IssuedKey:
    """签发一个 key，返回明文 token（只显示这一次）。"""
    limits.validate()
    key_id = uuid.uuid4().hex[:12]
    token = "sk-" + secrets.token_urlsafe(32)
    token_hash = sha256(token)
    now = now_iso()
    expires_at = None
    if limits.expires:
        expires_at = (datetime.now(timezone.utc) + _parse_duration(limits.expires)).isoformat(
            timespec="seconds"
        )

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO api_keys
               (id, project_id, token_hash, status, calls_total, tokens_total, expires_at,
                quota_cycle, provider, model, rps_limit, created_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key_id,
                limits.project_id,
                token_hash,
                "active",
                limits.calls,
                limits.tokens,
                expires_at,
                limits.cycle,
                limits.provider,
                limits.model,
                limits.rps_limit,
                now,
                limits.note,
            ),
        )
    return IssuedKey(
        id=key_id,
        token=token,
        token_hash_prefix=token_hash[:HASH_PREFIX_LEN],
        limits={
            "calls": limits.calls,
            "tokens": limits.tokens,
            "expires_at": expires_at,
            "cycle": limits.cycle,
            "provider": limits.provider,
            "model": limits.model,
            "project_id": limits.project_id,
            "rps_limit": limits.rps_limit,
            "note": limits.note,
        },
    )


def _row_to_status(row: sqlite3.Row) -> dict[str, Any]:
    used_ratio = None
    if row["calls_total"] is not None:
        used_ratio = row["calls_used"] / row["calls_total"]
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "calls": {"used": row["calls_used"], "total": row["calls_total"]},
        "tokens": {"used": row["tokens_used"], "total": row["tokens_total"]},
        "expires_at": row["expires_at"],
        "quota_cycle": row["quota_cycle"],
        "provider": row["provider"],
        "model": row["model"],
        "rps_limit": row["rps_limit"],
        "created_at": row["created_at"],
        "note": row["note"],
        "usage_ratio": used_ratio,
    }


def status(key_id: str) -> dict[str, Any] | None:
    """按公开 id 查余量（不暴露 token_hash）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        return _row_to_status(row) if row else None


def revoke(key_id: str) -> bool:
    """吊销 key。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET status='revoked' WHERE id=? AND status='active'",
            (key_id,),
        )
        if cur.rowcount == 1:
            conn.execute(
                "INSERT INTO usage_logs (key_id, action, detail, created_at) "
                "VALUES (?, 'revoke', 'manual revoke', ?)",
                (key_id, now_iso()),
            )
        return cur.rowcount == 1


def recharge(key_id: str, calls: int | None = None, tokens: int | None = None) -> bool:
    """给指定维度追加额度（只能加不能减）。"""
    if calls is None and tokens is None:
        raise ValueError("至少提供一个充值维度")
    with get_conn() as conn:
        cur = conn.execute("SELECT status FROM api_keys WHERE id = ?", (key_id,))
        row = cur.fetchone()
        if row is None or row["status"] != "active":
            return False
        if calls is not None:
            conn.execute(
                "UPDATE api_keys SET calls_total = COALESCE(calls_total, 0) + ? "
                "WHERE id = ? AND status='active'",
                (calls, key_id),
            )
        if tokens is not None:
            conn.execute(
                "UPDATE api_keys SET tokens_total = COALESCE(tokens_total, 0) + ? "
                "WHERE id = ? AND status='active'",
                (tokens, key_id),
            )
        conn.execute(
            "INSERT INTO usage_logs (key_id, action, amount, detail, created_at) "
            "VALUES (?, 'recharge', ?, ?, ?)",
            (key_id, (tokens or 0) + (calls or 0), f"+{calls} calls / +{tokens} tokens", now_iso()),
        )
        return True


def list_keys(
    status_filter: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """列出 key（不含 token_hash），返回 (列表, 总数)。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    clauses, params = [], []
    if status_filter in ("active", "revoked"):
        clauses.append("status = ?")
        params.append(status_filter)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM api_keys {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM api_keys {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return [_row_to_status(r) for r in rows], total


def logs(
    key_id: str,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """查某个 key 的审计日志（含 reserve 结算记录）。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        if row is None:
            return [], -1
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_logs WHERE key_id = ?", (key_id,)
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT action, amount, call_cost, detail, created_at FROM usage_logs "
            "WHERE key_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (key_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


# ---------- 项目 ----------


def create_project(
    name: str,
    owner: str | None = None,
    calls: int | None = None,
    tokens: int | None = None,
    balance: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """创建项目（共享额度池 + 可选余额）。"""
    if not name or not name.strip():
        raise ValueError("项目名不能为空")
    if calls is not None and calls < 1:
        raise ValueError("calls 至少为 1")
    if tokens is not None and tokens < 1:
        raise ValueError("tokens 至少为 1")
    if balance is not None and balance < 0:
        raise ValueError("balance 不能为负")
    project_id = uuid.uuid4().hex[:10]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, owner, note, status, calls_total, "
            "tokens_total, balance, created_at) VALUES (?,?,?,?,'active',?,?,?,?)",
            (project_id, name.strip(), owner, note, calls, tokens, balance, now_iso()),
        )
    return {"id": project_id, "name": name.strip(), "owner": owner, "note": note,
            "calls_total": calls, "tokens_total": tokens, "balance": balance}


def list_projects() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
    return [_project_to_dict(r) for r in rows]


def project_status(project_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return _project_to_dict(row) if row else None


def _project_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "owner": row["owner"],
        "note": row["note"],
        "status": row["status"],
        "calls": {"used": row["calls_used"], "total": row["calls_total"]},
        "tokens": {"used": row["tokens_used"], "total": row["tokens_total"]},
        "balance": row["balance"],
        "created_at": row["created_at"],
    }


def recharge_project(
    project_id: str,
    calls: int | None = None,
    tokens: int | None = None,
    balance: float | None = None,
) -> bool:
    """给项目追加共享池额度或充值余额（只加不减）。"""
    if calls is None and tokens is None and balance is None:
        raise ValueError("至少提供一个充值维度")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            return False
        if calls is not None:
            conn.execute(
                "UPDATE projects SET calls_total = COALESCE(calls_total, 0) + ? "
                "WHERE id = ? AND status='active'",
                (calls, project_id),
            )
        if tokens is not None:
            conn.execute(
                "UPDATE projects SET tokens_total = COALESCE(tokens_total, 0) + ? "
                "WHERE id = ? AND status='active'",
                (tokens, project_id),
            )
        if balance is not None:
            conn.execute(
                "UPDATE projects SET balance = COALESCE(balance, 0) + ? "
                "WHERE id = ? AND status='active'",
                (balance, project_id),
            )
        return True


# ---------- 吊销重签 ----------


def rotate(key_id: str) -> IssuedKey | None:
    """吊销旧 key 并按原限额签发新 key（单事务，崩溃不丢 key）。

    - 剩余额度语义：新 key 预扣旧 key 的已用量；
    - 剩余有效期保留：直接沿用旧 key 的 expires_at（绝对时间）。
    """
    old = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id = ? AND status = 'active'", (key_id,)
        ).fetchone()
        if row is None:
            return None
        old = dict(row)

        # 1) 吊销旧 key（同事务，原子）
        conn.execute(
            "UPDATE api_keys SET status='revoked' WHERE id = ? AND status='active'",
            (key_id,),
        )
        conn.execute(
            "INSERT INTO usage_logs (key_id, action, detail, created_at) "
            "VALUES (?, 'revoke', 'rotate', ?)",
            (key_id, now_iso()),
        )

        # 2) 生成新 key（沿用旧配置 + 剩余有效期）
        new_id = uuid.uuid4().hex[:12]
        token = "sk-" + secrets.token_urlsafe(32)
        token_hash = sha256(token)
        conn.execute(
            """INSERT INTO api_keys
               (id, project_id, token_hash, status, calls_total, tokens_total, expires_at,
                quota_cycle, provider, model, rps_limit, created_at, note)
               VALUES (?,?,?, 'active', ?,?,?,?,?,?,?,?,?)""",
            (
                new_id,
                old["project_id"],
                token_hash,
                old["calls_total"],
                old["tokens_total"],
                old["expires_at"],      # 剩余有效期（绝对时间）直接沿用
                old["quota_cycle"],
                old["provider"],
                old["model"],
                old["rps_limit"],
                now_iso(),
                (old["note"] or "") + " (rotated)",
            ),
        )

        # 3) 预扣旧 key 已用量 → "剩余额度"语义
        conn.execute(
            "UPDATE api_keys SET calls_used = calls_used + ?, tokens_used = tokens_used + ? "
            "WHERE id = ?",
            (old["calls_used"] or 0, old["tokens_used"] or 0, new_id),
        )
        conn.execute(
            "INSERT INTO usage_logs (key_id, action, detail, created_at) "
            "VALUES (?, 'issue', 'rotate', ?)",
            (new_id, now_iso()),
        )
    return IssuedKey(
        id=new_id,
        token=token,
        token_hash_prefix=token_hash[:HASH_PREFIX_LEN],
        limits={
            "calls": old["calls_total"],
            "tokens": old["tokens_total"],
            "expires_at": old["expires_at"],
            "cycle": old["quota_cycle"],
            "provider": old["provider"],
            "model": old["model"],
            "project_id": old["project_id"],
            "rps_limit": old["rps_limit"],
            "note": (old["note"] or "") + " (rotated)",
        },
    )
