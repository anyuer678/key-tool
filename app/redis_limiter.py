"""Redis 滑动窗口限流（多进程/多实例共享计数）。

设置环境变量 REDIS_URL 后自动启用（如 redis://127.0.0.1:6379/0）；
未设置时回退到单进程内存限流。测试用 fakeredis 验证。
"""

from __future__ import annotations

import os
import time
import uuid


def redis_client_from_env() -> object | None:
    """从 REDIS_URL 创建客户端；未配置返回 None。"""
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis as redis_lib

        return redis_lib.from_url(url)
    except Exception:
        return None


class RedisSlidingWindowLimiter:
    """基于 ZSET 的滑动窗口：窗口内计数 <= limit 才放行。"""

    PREFIX = "keytool:rl:"

    def __init__(self, client, max_rps: int = 20):
        self.client = client
        self.max_rps = max_rps

    def allow(self, key: str, max_rps: int | None = None) -> bool:
        limit = max_rps or self.max_rps
        now = int(time.time() * 1000)
        zkey = self.PREFIX + key
        member = f"{now}:{uuid.uuid4().hex[:8]}"  # 唯一 member，防同毫秒覆盖
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(zkey, 0, now - 1000)  # 清 1s 前
        pipe.zadd(zkey, {member: now})
        pipe.zcard(zkey)
        pipe.expire(zkey, 3)
        _, _, count, _ = pipe.execute()
        return count <= limit

    def reset(self, key: str | None = None) -> None:
        if key is None:
            return  # 不清全库
        self.client.delete(self.PREFIX + key)
