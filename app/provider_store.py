"""服务商存储：DB 化（providers 表）+ Fernet 加密 api_key + 内置预设。

- api_key 用 KEYTOOL_SECRET_KEY（Fernet key）加密存储；未配置时明文（本地模式）；
- api_key 支持 `${ENV_VAR}` 环境变量引用，读取时解析；
- 首次启动（DB 为空）自动内置常用服务商预设（DeepSeek/OpenAI/Ollama 等），
  用户只需在界面补 api_key。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet

from .db import get_conn, init_db

ENV_SECRET = "KEYTOOL_SECRET_KEY"
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# 内置预设：OpenAI 兼容服务商，api_key 用环境变量引用，未设置则为空（页面提示补 key）
PRESETS: list[dict] = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "api_key": "${DEEPSEEK_API_KEY}", "default_model": "deepseek-chat",
     "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "api_key": "${OPENAI_API_KEY}", "default_model": "gpt-4o-mini",
     "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "o3-mini"]},
    {"id": "ollama", "name": "Ollama 本地", "base_url": "http://127.0.0.1:11434/v1",
     "api_key": "ollama", "default_model": "qwen2.5",
     "models": ["qwen2.5", "llama3.1", "gemma2"]},
    {"id": "kimi", "name": "Moonshot Kimi", "base_url": "https://api.moonshot.cn/v1",
     "api_key": "${MOONSHOT_API_KEY}", "default_model": "moonshot-v1-8k",
     "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "api_key": "${ZHIPU_API_KEY}", "default_model": "glm-4-flash",
     "models": ["glm-4-flash", "glm-4-plus"]},
    {"id": "qwen", "name": "阿里 Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "api_key": "${DASHSCOPE_API_KEY}", "default_model": "qwen-plus",
     "models": ["qwen-plus", "qwen-max", "qwen-turbo"]},
]


def _fernet() -> Fernet | None:
    key = os.environ.get(ENV_SECRET)
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except Exception:
        return None


def encryption_enabled() -> bool:
    return _fernet() is not None


def _encrypt(plain: str) -> str:
    f = _fernet()
    if f is None:
        return plain  # 本地模式：明文
    return "enc:" + f.encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(stored: str) -> str:
    if not stored.startswith("enc:"):
        return stored
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(stored[4:].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _resolve_env_ref(api_key: str) -> str:
    """解析 ${ENV_VAR} 引用：环境变量存在则替换，否则返回空。"""
    m = _ENV_REF.match(api_key.strip())
    if m:
        return os.environ.get(m.group(1), "")
    return api_key


def list_all() -> list[Provider]:
    from .providers import Provider

    ensure_schema()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM providers ORDER BY is_default DESC, name"
        ).fetchall()
    return [
        Provider(
            id=r["id"],
            name=r["name"],
            base_url=r["base_url"],
            api_key=_resolve_env_ref(_decrypt(r["api_key_enc"])),
            default_model=r["default_model"] or "",
            models=json.loads(r["models_json"] or "[]"),
        )
        for r in rows
    ]


def update_key(provider_id: str, api_key: str) -> bool:
    """只更新服务商 api_key（页面「填 key」用）。"""
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE providers SET api_key_enc = ? WHERE id = ?",
            (_encrypt(api_key), provider_id),
        )
        return cur.rowcount == 1


def seed_presets() -> int:
    """DB 为空时内置预设服务商，返回新增数量。"""
    from .providers import Provider

    existing = {p.id for p in list_all()}
    added = 0
    for item in PRESETS:
        if item["id"] in existing:
            continue
        p = Provider(
            id=item["id"], name=item["name"], base_url=item["base_url"],
            api_key=item["api_key"], default_model=item["default_model"],
            models=list(item["models"]),
        )
        upsert(p, is_default=(added == 0))
        added += 1
    return added


def presets_missing() -> list[dict]:
    """返回尚未添加的内置预设（供页面「一键添加」）。"""
    existing = {p.id for p in list_all()}
    return [p for p in PRESETS if p["id"] not in existing]


def upsert(p: Provider, is_default: bool = False) -> None:
    ensure_schema()
    with get_conn() as conn:
        if is_default:
            conn.execute("UPDATE providers SET is_default = 0")
        conn.execute(
            "INSERT INTO providers (id, name, base_url, api_key_enc, default_model, "
            "models_json, is_default) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, base_url=excluded.base_url, "
            "api_key_enc=excluded.api_key_enc, default_model=excluded.default_model, "
            "models_json=excluded.models_json, is_default=excluded.is_default",
            (p.id, p.name, p.base_url, _encrypt(p.api_key),
             p.default_model, json.dumps(p.models, ensure_ascii=False),
             1 if is_default else 0),
        )


def delete(provider_id: str) -> bool:
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        return cur.rowcount == 1


def default_id() -> str | None:
    ensure_schema()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM providers WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute("SELECT id FROM providers LIMIT 1").fetchone()
        return row["id"] if row else None


def ensure_schema() -> None:
    """建 providers 表（含加密存储字段）。"""
    with get_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS providers (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                base_url      TEXT NOT NULL,
                api_key_enc   TEXT NOT NULL,
                default_model TEXT,
                models_json   TEXT DEFAULT '[]',
                is_default    INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT
            )"""
        )


def seed_from_json(path: str | Path | None = None) -> int:
    """DB 为空且 providers.json 存在时种子导入，返回导入数量。"""
    from .providers import load_config

    if list_all():
        return 0
    providers, default = load_config(path)
    for p in providers:
        upsert(p, is_default=(p.id == default))
    return len(providers)


def init_provider_store() -> None:
    ensure_schema()
    seed_from_json()
    seed_presets()
    if not encryption_enabled():
        # 检测是否有加密存储的数据：有则不可恢复，必须醒目提示
        with get_conn() as conn:
            enc_count = conn.execute(
                "SELECT COUNT(*) AS n FROM providers WHERE api_key_enc LIKE 'enc:%'"
            ).fetchone()["n"]
        if enc_count:
            print(f"[错误] 发现 {enc_count} 个服务商以加密形式存储，但未配置 KEYTOOL_SECRET_KEY")
            print("       api_key 将无法解密（不可恢复）。请恢复正确的 KEYTOOL_SECRET_KEY 后重启，")
            print("       或删除这些服务商后重新添加。")
        else:
            print("[警告] 未设置环境变量 KEYTOOL_SECRET_KEY，服务商 api_key 将以明文存储")
            print("       生产环境请设置 KEYTOOL_SECRET_KEY（Fernet key）后启动")
