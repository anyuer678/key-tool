"""pytest 配置：测试使用独立临时 SQLite 库 + mock 上游服务商。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

# 在导入 app.main（模块级 init_db）之前切换数据库路径
_TMP = Path(tempfile.mkdtemp(prefix="keytool-test-"))

from app.db import init_db, set_db_path  # noqa: E402

set_db_path(_TMP / "test.db")
init_db()

from app.providers import Provider, registry  # noqa: E402

from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _upstream_handler(status: int = 200, usage: int = 100):
    """OpenAI 兼容上游 mock：可配置状态码与 usage。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(
                status, json={"error": {"message": f"upstream error {status}"}}
            )
        body = json.loads(request.content)
        return httpx.Response(
            status,
            json={
                "id": "chatcmpl-mock",
                "model": body.get("model", "fake-model"),
                "choices": [
                    {"message": {"role": "assistant", "content": "mock reply"}}
                ],
                "usage": {"total_tokens": usage},
            },
        )
    return handler


def _make_provider(handler) -> Provider:
    return Provider(
        id="fake",
        name="Fake",
        base_url="https://fake.local/v1",
        api_key="test-key",
        default_model="fake-model",
        models=["fake-model", "fake-model-2"],
        transport=httpx.MockTransport(handler),
    )


# 默认注入一个正常上游（usage=100）
registry.set([_make_provider(_upstream_handler(200, 100))], default_id="fake")


@pytest.fixture()
def client():
    from app.db import get_conn
    from app.security import issue_limiter
    from app.middleware import limiter

    with get_conn() as conn:
        conn.execute("DELETE FROM usage_logs")
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM projects")
    issue_limiter.reset()
    limiter.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def live_server():
    """真实 uvicorn 服务（与测试库同一进程/同一 DB），供 CLI 测试直连。"""
    import socket
    import threading
    import time

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("uvicorn 启动失败")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def mock_upstream():
    """切换 mock 上游行为：mock_upstream(status=500) / (usage=42)。"""
    def inject(status: int = 200, usage: int = 100):
        registry.set([_make_provider(_upstream_handler(status, usage))], default_id="fake")
        return registry.get("fake")

    yield inject


@pytest.fixture(autouse=True)
def _restore_registry():
    """每个测试结束后恢复 registry 快照，避免测试间污染。"""
    snapshot = dict(registry._providers)
    default_id = registry.default_id
    yield
    registry._providers = snapshot
    registry.default_id = default_id
