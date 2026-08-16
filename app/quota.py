"""额度扣减核心：原子条件 UPDATE 实现"预扣 + 退差 + 懒重置 + 双层扣减 + 余额计费"。

设计要点（v0.4）：
- 单事务内先扣 key 层、再扣项目共享池（calls/tokens/balance），任一失败整体回滚；
- key 层含懒重置（quota_cycle）；项目池不含周期重置；
- reserve 预扣 token 预估上限与预估金额 → settle 按实际退差（token + 金额）→
  5xx 走 refund 全额退（token + 次数 + 金额）；
- 计费：pricing 表按 provider+model 定价（元/1M tokens），项目 balance 为 NULL 时不
  计费（cost 恒为 0）；balance 预扣保证不超支；
- 幂等/并发安全：结算时原子抢占预扣记录（reserve → settling），仅一次生效；
- 每笔动作写 usage_logs 审计（含 project_id 与 cost，供项目维度统计）。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .db import get_conn
from .keys import current_cycle, now_iso, sha256

SETTLE_TIMEOUT_S = 60.0  # 预留票据有效期，超时按预扣结算


@dataclass
class ReserveResult:
    ok: bool
    reason: str | None  # None=成功；否则为失败原因
    key_id: str | None
    project_id: str | None
    reservation_id: str | None
    reserved_tokens: int
    reserved_cost: float  # 预扣金额（元）；项目不计费时为 0


class QuotaExceeded(Exception):
    """额度不足/已过期/已吊销。reason 用于映射 HTTP 状态码。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# key 层原子扣减 SQL：所有维度校验 + 懒重置 + 扣减，一个语句完成。
_KEY_RESERVE_SQL = """
UPDATE api_keys
SET    calls_used  = (CASE WHEN quota_cycle IS NOT NULL AND quota_cycle <> :cycle
                           THEN 0 ELSE calls_used END) + :call_cost,
       tokens_used = (CASE WHEN quota_cycle IS NOT NULL AND quota_cycle <> :cycle
                           THEN 0 ELSE tokens_used END) + :reserve_tokens,
       quota_cycle = CASE WHEN quota_cycle IS NOT NULL AND quota_cycle <> :cycle
                          THEN :cycle ELSE quota_cycle END
WHERE  token_hash = :hash
  AND  status = 'active'
  AND  (expires_at IS NULL OR expires_at > :now)
  AND  (calls_total IS NULL
        OR (CASE WHEN quota_cycle IS NOT NULL AND quota_cycle <> :cycle
                 THEN 0 ELSE calls_used END) + :call_cost <= calls_total)
  AND  (tokens_total IS NULL
        OR (CASE WHEN quota_cycle IS NOT NULL AND quota_cycle <> :cycle
                 THEN 0 ELSE tokens_used END) + :reserve_tokens <= tokens_total)
"""

# 项目共享池原子扣减：池配额 + 余额预扣，一个语句完成。
_PROJECT_RESERVE_SQL = """
UPDATE projects
SET    calls_used  = calls_used  + :call_cost,
       tokens_used = tokens_used + :reserve_tokens,
       balance     = CASE WHEN balance IS NULL THEN NULL ELSE balance - :cost END
WHERE  id = :project_id AND status = 'active'
  AND  (calls_total  IS NULL OR calls_used  + :call_cost <= calls_total)
  AND  (tokens_total IS NULL OR tokens_used + :reserve_tokens <= tokens_total)
  AND  (balance IS NULL OR balance >= :cost)
"""


def _price_for(provider: str | None, model: str | None) -> float:
    """查定价（元/1M tokens）；未配置或参数缺失返回 0。"""
    if not provider or not model:
        return 0.0
    with get_conn() as conn:
        row = conn.execute(
            "SELECT price_per_million FROM pricing WHERE provider=? AND model=?",
            (provider, model),
        ).fetchone()
    return float(row["price_per_million"]) if row else 0.0


def reserve(
    token: str,
    reserve_tokens: int = 0,
    call_cost: int = 1,
    provider: str | None = None,
    model: str | None = None,
) -> ReserveResult:
    """消费入口：预扣（次数 +1，token 与金额按预估上限）。

    返回 ReserveResult；失败时 reason 说明原因。成功后调用方必须在
    SETTLE_TIMEOUT_S 内 settle/refund，否则按预扣结算。
    """
    if reserve_tokens < 0 or call_cost < 1:
        return ReserveResult(False, "invalid_params", None, None, None, 0, 0.0)

    price = _price_for(provider, model)
    cost = round(reserve_tokens * price / 1_000_000, 6)
    now = now_iso()
    cycle = current_cycle()
    reservation_id = uuid.uuid4().hex

    try:
        with get_conn() as conn:
            cur = conn.execute(
                _KEY_RESERVE_SQL,
                {
                    "hash": sha256(token),
                    "cycle": cycle,
                    "now": now,
                    "call_cost": call_cost,
                    "reserve_tokens": reserve_tokens,
                },
            )
            if cur.rowcount != 1:
                row = conn.execute(
                    "SELECT id, status, expires_at, calls_total, calls_used, "
                    "tokens_total, tokens_used, quota_cycle FROM api_keys "
                    "WHERE token_hash = ?",
                    (sha256(token),),
                ).fetchone()
                return ReserveResult(
                    False, _classify_key_reject(row, now, call_cost, reserve_tokens),
                    row["id"] if row else None, None, None, 0, 0.0,
                )

            key_row = conn.execute(
                "SELECT id, project_id FROM api_keys WHERE token_hash = ?",
                (sha256(token),),
            ).fetchone()
            key_id = key_row["id"]
            project_id = key_row["project_id"]

            if project_id is not None:
                cur2 = conn.execute(
                    _PROJECT_RESERVE_SQL,
                    {
                        "project_id": project_id,
                        "call_cost": call_cost,
                        "reserve_tokens": reserve_tokens,
                        "cost": cost,
                    },
                )
                if cur2.rowcount != 1:
                    prow = conn.execute(
                        "SELECT status, calls_total, calls_used, tokens_total, "
                        "tokens_used, balance FROM projects WHERE id = ?",
                        (project_id,),
                    ).fetchone()
                    raise QuotaExceeded(
                        _classify_project_reject(prow, call_cost, reserve_tokens, cost)
                    )

            conn.execute(
                "INSERT INTO usage_logs "
                "(key_id, project_id, action, amount, call_cost, cost, detail, created_at) "
                "VALUES (?, ?, 'reserve', ?, ?, ?, ?, ?)",
                (key_id, project_id, reserve_tokens, call_cost, cost, reservation_id, now),
            )
    except QuotaExceeded as e:
        return ReserveResult(False, e.reason, key_id, project_id, None, 0, 0.0)

    return ReserveResult(
        True, None, key_id, project_id, reservation_id, reserve_tokens, cost
    )


def _classify_key_reject(
    row: sqlite3.Row | None,
    now: str,
    call_cost: int = 1,
    reserve_tokens: int = 0,
) -> str:
    """按"预扣后是否超限"分类 key 层拒绝原因（含懒重置归零判断）。"""
    if row is None:
        return "invalid_key"
    if row["status"] != "active":
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    reset = row["quota_cycle"] is not None and row["quota_cycle"] != current_cycle()
    used_calls = 0 if reset else row["calls_used"]
    used_tokens = 0 if reset else row["tokens_used"]
    if row["calls_total"] is not None and used_calls + call_cost > row["calls_total"]:
        return "out_of_calls"
    if (
        row["tokens_total"] is not None
        and used_tokens + reserve_tokens > row["tokens_total"]
    ):
        return "out_of_tokens"
    return "quota_exceeded"


def _classify_project_reject(
    prow: sqlite3.Row | None,
    call_cost: int = 1,
    reserve_tokens: int = 0,
    cost: float = 0.0,
) -> str:
    if prow is None:
        return "project_not_found"
    if prow["status"] != "active":
        return "project_archived"
    if (
        prow["calls_total"] is not None
        and prow["calls_used"] + call_cost > prow["calls_total"]
    ):
        return "project_out_of_calls"
    if (
        prow["tokens_total"] is not None
        and prow["tokens_used"] + reserve_tokens > prow["tokens_total"]
    ):
        return "project_out_of_tokens"
    if prow["balance"] is not None and prow["balance"] < cost:
        return "project_insufficient_balance"
    return "project_quota_exceeded"


def _settle_common(
    reservation_id: str, delta_tokens: int, delta_cost: float, action: str
) -> bool:
    """按 reservation 应用差额（settle 退差、refund 全额退），key 与项目池同步。

    幂等/并发安全：先原子抢占预扣记录（reserve → settling），仅一次生效；
    预扣记录改名保留用于审计。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE usage_logs SET action='settling' "
            "WHERE action='reserve' AND detail = ?",
            (reservation_id,),
        )
        if cur.rowcount != 1:
            return False  # 已结算/已退款（幂等）

        meta = conn.execute(
            "SELECT key_id, project_id, amount, call_cost, cost FROM usage_logs "
            "WHERE action='settling' AND detail = ?",
            (reservation_id,),
        ).fetchone()
        key_id, project_id = meta["key_id"], meta["project_id"]
        call_cost = meta["call_cost"] or 0

        # key 层：token 差额；refund 时次数也退
        if action == "refund":
            conn.execute(
                "UPDATE api_keys SET tokens_used = tokens_used + ?, "
                "calls_used = calls_used - ? WHERE id = ?",
                (delta_tokens, call_cost, key_id),
            )
        else:
            conn.execute(
                "UPDATE api_keys SET tokens_used = tokens_used + ? WHERE id = ?",
                (delta_tokens, key_id),
            )

        # 项目层：token 差额 + 金额差额（余额钳制不小于 0，防御超扣）
        if project_id is not None:
            if action == "refund":
                conn.execute(
                    "UPDATE projects SET tokens_used = tokens_used + ?, "
                    "calls_used = calls_used - ?, "
                    "balance = CASE WHEN balance IS NULL THEN NULL "
                    "ELSE MAX(balance + ?, 0) END "
                    "WHERE id = ?",
                    (delta_tokens, call_cost, delta_cost, project_id),
                )
            else:
                conn.execute(
                    "UPDATE projects SET tokens_used = tokens_used + ?, "
                    "balance = CASE WHEN balance IS NULL THEN NULL "
                    "ELSE MAX(balance + ?, 0) END "
                    "WHERE id = ?",
                    (delta_tokens, delta_cost, project_id),
                )

        # 预扣记录改名保留（审计）
        conn.execute(
            "UPDATE usage_logs SET action=? "
            "WHERE action='settling' AND detail = ?",
            (f"{action}_reserved", reservation_id),
        )
        conn.execute(
            "INSERT INTO usage_logs "
            "(key_id, project_id, action, amount, call_cost, cost, detail, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (key_id, project_id, action, delta_tokens, delta_cost, reservation_id, now_iso()),
        )
    return True


def settle(key_id: str, reservation_id: str, actual_tokens: int) -> bool:
    """成功后按实际 token 结算：key/项目池退差 + 余额按实际金额退差。

    防御：actual_tokens 超过预扣量（上游不守 max_tokens 等异常）时按预扣量
    封顶结算并写 warn 审计，避免 key 配额与项目余额被扣成负值。
    """
    if actual_tokens < 0:
        return False
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT amount, cost, project_id FROM usage_logs "
            "WHERE action='reserve' AND detail = ?",
            (reservation_id,),
        ).fetchone()
        if meta is None:
            return False
        reserved, reserved_cost = meta["amount"], meta["cost"]
    if reserved <= 0:
        return False

    capped = min(actual_tokens, reserved)
    delta_tokens = capped - reserved
    # 实际金额 = 实际 × (预扣单价)；退差 = 预扣金额 - 实际金额
    actual_cost = round(capped * (reserved_cost / reserved), 6)
    delta_cost = round(reserved_cost - actual_cost, 6)
    ok = _settle_common(reservation_id, delta_tokens, delta_cost, "settle")
    if ok and capped < actual_tokens:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, project_id, action, amount, call_cost, cost, detail, created_at) "
                "VALUES (?, ?, 'warn', ?, 0, 0, ?, ?)",
                (key_id, meta["project_id"], actual_tokens - capped,
                 f"actual({actual_tokens}) > reserved({reserved}), capped",
                 now_iso()),
            )
    return ok


def refund(reservation_id: str) -> bool:
    """5xx/服务端故障：全额退回预扣 token、次数与金额（2xx 才结算）。"""
    with get_conn() as conn:
        meta = conn.execute(
            "SELECT amount, cost FROM usage_logs "
            "WHERE action='reserve' AND detail = ?",
            (reservation_id,),
        ).fetchone()
        if meta is None:
            return False
        reserved, reserved_cost = meta["amount"], meta["cost"]
    return _settle_common(reservation_id, -reserved, reserved_cost, "refund")

