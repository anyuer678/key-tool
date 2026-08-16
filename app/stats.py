"""用量统计：按天/项目聚合 usage_logs（成功消费的 calls / tokens / 金额）。"""

from __future__ import annotations

from typing import Any

from .db import get_conn


def usage_stats(
    project_id: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """按天聚合成功消费。

    - calls：settle 记录条数（成功调用次数）；
    - tokens：reserve.amount（预扣额）+ settle.amount（退差，负）＝ 实际用量；
    - cost：-settle.cost（实际扣费金额，元）。
    """
    days = max(1, min(days, 365))
    clauses, params = ["s.action = 'settle'", "s.created_at >= ?"], [f"-{days} days"]
    if project_id:
        clauses.append("s.project_id = ?")
        params.append(project_id)

    where = " AND ".join(clauses)
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT substr(s.created_at, 1, 10) AS day,
                       COUNT(*)                  AS calls,
                       SUM(r.amount + s.amount)  AS tokens,
                       SUM(r.cost - s.cost)      AS cost
                FROM usage_logs s
                JOIN usage_logs r
                  ON r.action = 'settle_reserved' AND r.detail = s.detail
                WHERE {where}
                GROUP BY day ORDER BY day""",
            params,
        ).fetchall()
    items = []
    for r in rows:
        items.append({
            "date": r["day"],
            "calls": r["calls"],
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 6),
        })

    # 汇总
    total = {
        "calls": sum(x["calls"] for x in items),
        "tokens": sum(x["tokens"] for x in items),
        "cost": round(sum(x["cost"] for x in items), 6),
    }
    return {"days": items, "total": total}


def project_totals(project_id: str | None = None) -> dict[str, Any]:
    """全量累计（不限时间窗）：calls / tokens / cost。"""
    clauses, params = ["s.action = 'settle'"], []
    if project_id:
        clauses.append("s.project_id = ?")
        params.append(project_id)
    where = " AND ".join(clauses)
    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*)                 AS calls,
                       SUM(r.amount + s.amount) AS tokens,
                       SUM(r.cost - s.cost)     AS cost
                FROM usage_logs s
                JOIN usage_logs r
                  ON r.action = 'settle_reserved' AND r.detail = s.detail
                WHERE {where}""",
            params,
        ).fetchone()
    return {
        "calls": row["calls"] or 0,
        "tokens": int(row["tokens"] or 0),
        "cost": round(float(row["cost"] or 0), 6),
    }
