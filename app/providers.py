"""服务商配置：任意 OpenAI 兼容服务。

配置来源：项目根目录 providers.json（可选）。api_key 支持 ${ENV_VAR} 语法
从环境变量读取，避免密钥明文落盘。未配置任何服务商时，服务照常运行，
但 /v1/chat 返回 503（无法转发）。

providers.json 示例：
{
  "default": "deepseek",
  "providers": [
    {
      "id": "deepseek",
      "name": "DeepSeek",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "${DEEPSEEK_API_KEY}",
      "default_model": "deepseek-chat"
    }
  ]
}
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "providers.json"

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass
class Provider:
    id: str
    name: str
    base_url: str
    api_key: str
    default_model: str
    # 预设模型列表（供签发页下拉选择）；空则仅 default_model
    models: list[str] = field(default_factory=list)
    # 测试注入用：httpx transport（如 MockTransport），None = 走真实网络
    transport: Any = field(default=None, repr=False, compare=False)


def _resolve_env(value: str) -> str:
    m = _ENV_REF.match(value.strip())
    if m:
        env_name = m.group(1)
        resolved = os.environ.get(env_name, "")
        if not resolved:
            raise ValueError(f"环境变量 {env_name} 未设置（providers.json 中引用）")
        return resolved
    return value


def load_config(path: str | Path | None = None) -> tuple[list[Provider], str | None]:
    """加载 providers.json，返回 (providers, 配置的 default id)。"""
    path = Path(path) if path is not None else CONFIG_PATH
    if not path.exists():
        return [], None
    data = json.loads(path.read_text(encoding="utf-8"))
    providers: list[Provider] = []
    for item in data.get("providers", []):
        default_model = item.get("default_model", "")
        models = list(item.get("models", [])) or ([default_model] if default_model else [])
        providers.append(
            Provider(
                id=item["id"],
                name=item.get("name", item["id"]),
                base_url=item["base_url"].rstrip("/"),
                api_key=_resolve_env(item["api_key"]),
                default_model=default_model,
                models=models,
            )
        )
    return providers, data.get("default")


class ProviderRegistry:
    """模块级单例：启动时加载一次，测试可替换。"""

    def __init__(self, path: str | Path | None = None, default_id: str | None = None):
        self._providers: dict[str, Provider] = {}
        self.default_id: str | None = None
        self.reload(path, default_id)

    def reload(self, path: str | Path | None = None, default_id: str | None = None) -> None:
        """从 DB 加载服务商（v0.4 起服务商由 Web 管理，DB 为唯一事实源）。

        保留 path 参数仅为兼容测试；优先 DB，DB 空则回退 JSON 种子。
        """
        from . import provider_store

        providers = provider_store.list_all()
        if not providers and path is not None:
            providers, cfg_default = load_config(path)
            if cfg_default and default_id is None:
                default_id = cfg_default
        self._providers = {p.id: p for p in providers}
        if default_id and default_id in self._providers:
            self.default_id = default_id
        else:
            self.default_id = provider_store.default_id()

    def get(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def default(self) -> Provider | None:
        return self._providers.get(self.default_id) if self.default_id else None

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def set(self, providers: list[Provider], default_id: str | None = None) -> None:
        """测试注入：直接设置 provider 列表（跳过文件加载）。"""
        self._providers = {p.id: p for p in providers}
        self.default_id = default_id or (providers[0].id if providers else None)


registry = ProviderRegistry()
