"""FastMCP server 组装与 ASGI 应用构建。

路由：
    GET /health   存活探针（免认证）
    /             FastMCP streamable_http_app（/mcp）

认证可选：AUTH_TOKEN 非空时挂 FastMCP 原生 token_verifier；为空则不启用。
结构对齐 applog-mcp-server（v1 不做 metrics / 限流 / Origin-Host 中间件）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from aiops_datasource_mcp_server import __version__
from aiops_datasource_mcp_server.backends.prometheus import available_metrics
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.tools import register

logger = logging.getLogger("aiops_datasource_mcp_server.server")


def _build_fastmcp():
    """构造 FastMCP 实例（原生认证为可选项 + JSON response）。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    settings = get_settings()

    kwargs: dict[str, Any] = {
        "name": "aiops-datasource-mcp-server",
        "json_response": True,
        "instructions": (
            "AIOps 数据源只读查询工具：Elasticsearch 日志、Prometheus 指标、"
            "Kubernetes 状态。\n"
            "**所有带时间的查询必须指定 start_time/end_time**（ISO8601），"
            "本 server 不提供无窗口的全量查询——诊断应聚焦故障发生的那一段时间。\n"
            f"Prometheus 指标为领域语义（非 PromQL 表达式），可用："
            f"{', '.join(available_metrics())}。\n"
            "所有工具均为只读查询，返回 {success, ...} 结构；无数据时字段为 null，"
            "不要当成 0。"
        ),
        # Origin/Host 防护由部署侧（nginx/网关）承担，关闭 FastMCP 内建防护避免双重校验
        "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False),
    }

    if settings.auth_token:
        from mcp.server.auth.settings import AuthSettings

        from aiops_datasource_mcp_server.auth.token_verifier import StaticTokenVerifier

        kwargs["auth"] = AuthSettings(
            issuer_url="https://aiops-datasource-mcp-server.invalid",
            resource_server_url=None,
        )
        kwargs["token_verifier"] = StaticTokenVerifier()

    return FastMCP(**kwargs)


def create_mcp_server():
    """创建 FastMCP 实例并注册全部工具。

    先做环境校验（production 强制认证），保证绕过 main() 直挂 ASGI 的部署也不能裸奔。
    """
    get_settings().validate_for_environment()
    mcp = _build_fastmcp()
    names = register(mcp)
    logger.info("registered %d tools: %s", len(names), ", ".join(names))
    return mcp


def _health(_request) -> JSONResponse:
    return JSONResponse(
        {"status": "ok", "version": __version__, "service": "aiops-datasource-mcp-server"}
    )


def build_app(mcp=None) -> Starlette:
    """构建最终 ASGI 应用（含 /health 与 MCP 端点）。"""
    server = mcp if mcp is not None else create_mcp_server()
    inner_app = server.streamable_http_app()

    routes = [
        Route("/health", _health, methods=["GET"]),
        Mount("/", app=inner_app),
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
