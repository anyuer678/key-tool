"""SQLite 数据层：api_keys / usage_logs 表结构与连接管理。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "keys.db"

# 测试/部署可覆盖默认库路径（调用 set_db_path 后所有连接走新路径）
_DB_PATH_OVERRIDE: Path | None = None


def set_db_path(path: str | Path) -> None:
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = Path(path)


def _resolve_db_path() -> Path:
    return _DB_PATH_OVERRIDE if _DB_PATH_OVERRIDE is not None else DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    owner        TEXT,
    note         TEXT,
    status       TEXT NOT NULL DEFAULT 'active',   -- active | archived
    -- 项目共享池（NULL = 不限）
    calls_total  INTEGER,
    calls_used   INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    -- 计费余额（元；NULL = 不计费）
    balance      REAL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,        -- 公开 ID（查余量/吊销用），非密钥
    project_id   TEXT REFERENCES projects(id),  -- 归属项目（NULL = 无项目）
    token_hash   TEXT NOT NULL UNIQUE,    -- SHA-256(token)，只存哈希
    status       TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    unit         TEXT NOT NULL DEFAULT 'mixed',    -- 维度说明，保留字段
    -- 次数维度
    calls_total  INTEGER,                 -- NULL = 不限
    calls_used   INTEGER NOT NULL DEFAULT 0,
    -- token 维度
    tokens_total INTEGER,                 -- NULL = 不限
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    -- 时间维度
    expires_at   TEXT,                    -- ISO 时间；NULL = 永久
    -- 策略绑定（NULL = 不限）
    provider     TEXT,                    -- 绑定服务商 id
    model        TEXT,                    -- 绑定模型名
    -- 周期重置（懒重置）
    quota_cycle  TEXT,                    -- 如 '2026-03'；NULL = 不重置
    -- 按 key 限流（NULL = 继承全局）
    rps_limit    INTEGER,
    created_at   TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id     TEXT NOT NULL,
    project_id TEXT,                       -- 归属项目（冗余，便于项目维度统计）
    action     TEXT NOT NULL,   -- reserve | settle | refund | revoke | recharge
    amount     INTEGER NOT NULL DEFAULT 0,  -- 本次变动的 token 数（正=扣，负=退）
    call_cost  INTEGER NOT NULL DEFAULT 0,  -- 本次调用算几次（成功=1）
    cost       REAL NOT NULL DEFAULT 0,     -- 本次计费金额（元，正=扣，负=退）
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing (
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    price_per_million REAL NOT NULL DEFAULT 0,  -- 元 / 1M tokens（input+output 合并）
    PRIMARY KEY (provider, model)
);

CREATE INDEX IF NOT EXISTS idx_logs_key ON usage_logs(key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_project ON usage_logs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_keys_project ON api_keys(project_id);
"""


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(db_path) if db_path is not None else _resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 串行写事务，避免 SQLITE_BUSY；演示规模足够
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def get_conn(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的库做增量迁移：按需 ALTER TABLE 补列。"""
    for table, cols in (
        ("api_keys", (("provider", "TEXT"), ("model", "TEXT"),
                      ("project_id", "TEXT"), ("rps_limit", "INTEGER"))),
        ("usage_logs", (("project_id", "TEXT"), ("cost", "REAL NOT NULL DEFAULT 0"))),
    ):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
