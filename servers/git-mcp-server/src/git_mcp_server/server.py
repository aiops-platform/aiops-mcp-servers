"""FastMCP server 组装与 ASGI 应用构建。

中间件栈（由外到内）：
    /health、/metrics 为公开端点（Starlette routes）
    / 下挂载：SecurityMiddleware → RateLimitMiddleware → FastMCP streamable_http_app
认证由 FastMCP 原生 token_verifier 在 /mcp 端点处理。
"""
from __future__ import annotations

import logging
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from git_mcp_server import __version__
from git_mcp_server.config import get_settings
from git_mcp_server.middleware.rate_limit import RateLimitMiddleware
from git_mcp_server.middleware.security import SecurityMiddleware
from git_mcp_server.tools import register

logger = logging.getLogger("git_mcp_server.server")


def _build_fastmcp():
    """构造 FastMCP 实例（v1 原生认证 + JSON response）。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    settings = get_settings()

    kwargs: dict[str, Any] = {
        "name": "git-mcp-server",
        "json_response": True,
        "instructions": (
            "Read-only access to Git repositories. "
            "All operations are non-mutating (status, log, show, branches, grep, blame). "
            "repo_path accepts either an absolute path within GIT_ALLOWED_ROOTS "
            "or a project name (an allowed root's basename or its direct child directory name)."
        ),
        # Origin/Host 校验由外层 SecurityMiddleware 统一实现（支持无端口 hostname）。
        # 关闭 FastMCP 内建 DNS rebinding 保护，避免双重校验与端口匹配歧义。
        "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False),
    }

    if settings.auth_token:
        from mcp.server.auth.settings import AuthSettings

        from git_mcp_server.auth.token_verifier import StaticTokenVerifier

        # 仅做 resource server 模式：token_verifier 存在 → /mcp 被 RequireAuthMiddleware 包裹。
        # issuer_url 为占位值（未启用 OAuth server 时不会被使用）。
        kwargs["auth"] = AuthSettings(
            issuer_url="https://git-mcp-server.invalid", resource_server_url=None
        )
        kwargs["token_verifier"] = StaticTokenVerifier()

    return FastMCP(**kwargs)


def create_mcp_server():
    """创建并注册工具后的 FastMCP 实例。"""
    mcp = _build_fastmcp()
    register(mcp)
    return mcp


def _health(_request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__, "service": "git-mcp-server"})


def _metrics(_request) -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def build_app(mcp=None) -> Starlette:
    """构建最终 ASGI 应用（含 /health、/metrics 与安全中间件）。

    安全/限流中间件仅包裹 MCP 端点（/health、/metrics 公开）。
    外层的 lifespan 显式委托内层 FastMCP app，以初始化 MCP 会话 task group。
    """
    from contextlib import asynccontextmanager

    server = mcp if mcp is not None else create_mcp_server()
    inner_app = server.streamable_http_app()

    guarded = SecurityMiddleware(RateLimitMiddleware(inner_app))

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/metrics", _metrics, methods=["GET"]),
        Mount("/", app=guarded),
    ]

    @asynccontextmanager
    async def _lifespan(app):
        async with inner_app.router.lifespan_context(app):
            yield

    return Starlette(routes=routes, lifespan=_lifespan)


def main() -> None:
    """入口：校验环境 → 构建应用 → 启动 uvicorn。"""
    import uvicorn

    settings = get_settings()
    settings.validate_for_environment()
    app = build_app()
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
