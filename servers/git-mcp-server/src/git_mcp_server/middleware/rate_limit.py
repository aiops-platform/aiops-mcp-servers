"""RateLimitMiddleware — 基于 Token Bucket 的 IP 级限流（纯 ASGI）。

- 默认禁用（RATE_LIMIT_ENABLED=false），启用后以客户端 IP 为键限流。
- 超限返回 HTTP 429，并附带 Retry-After。
- 在 HTTP 层覆盖 /health、/metrics、/mcp 全部端点。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from starlette.responses import JSONResponse

from git_mcp_server.config import get_settings

logger = logging.getLogger("git_mcp_server.rate_limit")


class TokenBucket:
    """线程安全的令牌桶。"""

    def __init__(self, capacity: int, refill_per_second: float):
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_per_second = refill_per_second
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def take(self) -> bool:
        """尝试消耗一个令牌；成功返回 True，失败返回 False。"""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class RateLimitMiddleware:
    def __init__(self, app):
        self.app = app
        settings = get_settings()
        self.enabled = settings.rate_limit_enabled
        capacity = max(1, settings.rate_limit_max_requests)
        window = max(1, settings.rate_limit_window_seconds)
        self.buckets: dict[str, TokenBucket] = defaultdict(
            lambda: TokenBucket(capacity=capacity, refill_per_second=capacity / window)
        )
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        client_ip = self._client_ip(scope)

        with self._lock:
            bucket = self.buckets[client_ip]
        if not bucket.take():
            retry_after = int(1.0 / max(bucket.refill_per_second, 1e-9))
            response = JSONResponse(
                {"error": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _client_ip(scope: dict) -> str:
        client = scope.get("client")
        if client and client[0]:
            return str(client[0])
        return "unknown"
