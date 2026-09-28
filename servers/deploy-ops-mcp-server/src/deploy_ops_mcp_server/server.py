"""FastMCP server 组装与 ASGI 应用构建。

路由：
    GET    /health     存活探针（免认证）
    /                  FastMCP streamable_http_app（/mcp）

认证可选：AUTH_TOKEN 非空时挂 FastMCP 原生 token_verifier；为空则不启用。
结构对齐 aiops-datasource-mcp-server（**无 admin 路由** —— 本 server 没有可编辑的数据面，
只读写集群）。

## 启动时做一次自检，探不到就**拒绝启动**

本 server 有一条**承重的拓扑约束**（见 `__init__.py`）：
它必须与 docker/podman daemon 跑在同一台机器上 —— 因为 `rollout_deployment` 第一步
`minikube image load` 要读宿主的镜像 store。

**为什么要在启动时探而不是等出错**：违反这条约束时，本 server 照常启动、照常接收请求，
症状只在**某次真的滚到一半**时出现 —— 而那时的报错是 `ImagePullBackOff` 或
`minikube image load 失败`，离"这台机器上没有 docker daemon"很远。
放在启动时，问题摆在装环境的时候（与 agentflow 的 `make doctor` 同一个理由）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from deploy_ops_mcp_server import __version__
from deploy_ops_mcp_server.backends.rollout import probe_runtime
from deploy_ops_mcp_server.config import get_settings
from deploy_ops_mcp_server.tools import register

logger = logging.getLogger("deploy_ops_mcp_server.server")


def _build_fastmcp():
    """构造 FastMCP 实例（原生认证为可选项 + JSON response）。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    settings = get_settings()

    kwargs: dict[str, Any] = {
        "name": "deploy-ops-mcp-server",
        "json_response": True,
        "instructions": (
            "集群发布工具。**本 server 只做一件事：把某个 Deployment 滚到指定镜像，"
            "并如实回报结果。**\n"
            "· `get_deployment_status(service)` —— 只读，读**从 pod 上取回**的镜像；\n"
            "· `rollout_deployment(service, image)` —— **写操作**，会真的改线上：\n"
            "  装镜像进节点 → set image → 等滚动 → 回读 pod 的镜像。\n"
            "**部署目标（namespace/deployment/container）由本 server 自己持有**，"
            "调用方只给 `service` 与 `image` —— 不要试图传命名空间。\n"
            "`success: false` 时**必须如实上报未部署**：滚不上去而工单报「已解决」，"
            "是这条链上最坏的形态。失败结果里的 `stage` 指明卡在哪一步，`error` 里"
            "带回了回滚命令（本 server **不自动回滚**）。"
        ),
        # Origin/Host 防护由部署侧（nginx/网关）承担，关闭 FastMCP 内建防护避免双重校验
        "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False),
    }

    if settings.auth_token:
        from mcp.server.auth.settings import AuthSettings

        from deploy_ops_mcp_server.auth.token_verifier import StaticTokenVerifier

        kwargs["auth"] = AuthSettings(
            issuer_url="https://deploy-ops-mcp-server.invalid",
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
        {"status": "ok", "version": __version__, "service": "deploy-ops-mcp-server"}
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
        # ⚠️ 自检在**真正开始服务之前**：探不到就抛，uvicorn 起不来。
        # 见模块 docstring —— 这条约束违反了不会当场炸，只会在某次 rollout 时才现形。
        ok, detail = await probe_runtime()
        if not ok:
            raise RuntimeError(f"deploy-ops 启动自检未通过：\n{detail}")
        logger.info("启动自检通过：%s", detail)
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
