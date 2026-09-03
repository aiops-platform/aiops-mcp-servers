"""SecurityMiddleware — Origin/Host 校验与请求日志（纯 ASGI，只包 /mcp）。

设计要点：
- 遵循 MCP 规范 MUST：Origin 头存在但不在白名单 → HTTP 403（防 DNS rebinding）。
- allowed_origins 为空 → 仅放行无 Origin 头的请求（fail-closed）。
- allowed_hosts 默认回环白名单，防 Host 头注入。
- 注入 trace_id 到 scope，便于日志与追踪串连。
"""
from __future__ import annotations

import logging
import uuid

from starlette.responses import JSONResponse

from git_mcp_server.config import get_settings

logger = logging.getLogger("git_mcp_server.security")


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path = scope.get("path", "")

        # --- trace_id ---
        trace_id = uuid.uuid4().hex[:16]
        scope["state"]["trace_id"] = trace_id

        # 安全校验仅作用于 MCP 端点（/health、/metrics 保持公开，供探活/监控）
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        # --- Host 校验（防 Host 头注入） ---
        headers = _headers(scope)
        host = headers.get("host")
        if host:
            hostname = host.split(":", 1)[0].strip().lower()
            if hostname and hostname not in settings.allowed_hosts_list:
                await _respond_403(send, receive, scope, "Host not allowed", trace_id)
                return

        # --- Origin 校验（防 DNS rebinding，MCP 规范 MUST） ---
        origin = headers.get("origin")
        if origin:
            allowed = settings.allowed_origins_list
            # 规范化比较：去掉末尾斜杠
            origin_normalized = origin.rstrip("/")
            allowed_normalized = [a.rstrip("/") for a in allowed]
            if origin_normalized not in allowed_normalized:
                await _respond_403(send, receive, scope, "Origin not allowed", trace_id)
                return

        # --- 请求日志（含 trace_id） ---
        path = scope.get("path", "")
        method = scope.get("method", "")
        logger.info("request trace_id=%s method=%s path=%s host=%s", trace_id, method, path, host)

        await self.app(scope, receive, send)


def _headers(scope: dict) -> dict:
    headers = {}
    raw = scope.get("headers", [])
    for key, value in raw:
        name = key.decode("latin-1").lower()
        headers[name] = value.decode("latin-1")
    return headers


async def _respond_403(send, receive, scope, reason: str, trace_id: str) -> None:
    message = "Request blocked by security policy"
    response = JSONResponse(
        {"error": message, "reason": reason, "trace_id": trace_id},
        status_code=403,
    )
    await response(scope, receive, send)
