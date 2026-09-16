"""FastMCP server 组装与 ASGI 应用构建。

路由：
    GET    /health                     存活探针（免认证）
    GET    /admin/cmdb/schema          表单 schema（给编辑界面用）
    GET    /admin/cmdb/summary         当前图概况
    GET    /admin/cmdb/nodes           节点清单
    POST   /admin/cmdb/nodes           新增节点
    PUT    /admin/cmdb/nodes/{id}      改节点
    DELETE /admin/cmdb/nodes/{id}      删节点（默认不级联，见 cmdb_admin）
    POST   /admin/cmdb/edges           新增边
    DELETE /admin/cmdb/edges/{id}      删边
    /                                  FastMCP streamable_http_app（/mcp）

认证可选：AUTH_TOKEN 非空时挂 FastMCP 原生 token_verifier；为空则不启用。
结构对齐 applog-mcp-server（v1 不做 metrics / 限流 / Origin-Host 中间件）。

**``/admin/cmdb/**`` 是唯一的写面**，与只读的 ``/mcp`` 分开挂：
- 未启用时**整组路由不注册**（不是注册了再返回 403）——不存在的路径比一个
  "理论上拒绝"的路径更难被绕过。
- 启用且有 AUTH_TOKEN 时，逐个请求校验 Bearer。
- 默认按环境开关，见 ``Settings.admin_enabled``。
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from aiops_datasource_mcp_server import __version__
from aiops_datasource_mcp_server.backends import cmdb_admin
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


# ----------------------------------------------------------------------
# CMDB 管理端点（唯一写面）
# ----------------------------------------------------------------------
def _admin_guard(request) -> JSONResponse | None:
    """认证闸门。返回非 None 表示已拒绝，调用方直接把它返回出去。"""
    token = get_settings().auth_token
    if not token:
        # 走到这里说明 admin_enabled 为真。development 默认可用（无 token），
        # production 下 validate_for_environment 已保证有 token，故这里不空转。
        return None
    header = request.headers.get("authorization", "")
    if header != f"Bearer {token}":
        return JSONResponse(
            {"error": "UNAUTHENTICATED", "message": "缺少或错误的 Authorization Bearer"},
            status_code=401,
        )
    return None


async def _json_body(request) -> dict:
    """解析请求体。空 body 当成 {}，让"只改一个字段"的调用也能过。"""
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise cmdb_admin.AdminError(f"请求体不是合法 JSON：{exc.msg}", status=400) from exc
    if not isinstance(parsed, dict):
        raise cmdb_admin.AdminError("请求体必须是 JSON 对象", status=400)
    return parsed


def _admin_route(handler):
    """把 ``cmdb_admin`` 的异常统一翻成 HTTP——否则一个没料到的异常会变成 500 空白。

    ``AdminError`` 自带 status（400/404/409/422）；其余异常照旧 500，但要带出
    真实原因，不然界面上只会看到一句 "Internal Server Error"。
    """

    async def _wrapped(request):
        denied = _admin_guard(request)
        if denied is not None:
            return denied
        try:
            return JSONResponse(await handler(request))
        except cmdb_admin.AdminError as exc:
            return JSONResponse(
                {"error": "ADMIN_REJECTED", "message": exc.message, **exc.details},
                status_code=exc.status,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：把真实原因带给调用方
            logger.exception("admin endpoint failed")
            return JSONResponse(
                {"error": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )

    return _wrapped


async def _admin_schema(request) -> dict:
    return cmdb_admin.describe_schema()


async def _admin_summary(request) -> dict:
    return cmdb_admin.summary()


async def _admin_list_nodes(request) -> dict:
    doc = cmdb_admin.read_doc()
    type_filter = request.query_params.get("type")
    nodes = doc.get("nodes") or {}
    if type_filter:
        nodes = {type_filter: nodes.get(type_filter, [])}
    return {
        "nodes": {k: v for k, v in nodes.items() if v} if not type_filter else nodes,
        "edges": doc.get("edges", []),
    }


async def _admin_create_node(request) -> dict:
    return cmdb_admin.create_node(await _json_body(request))


async def _admin_update_node(request) -> dict:
    return cmdb_admin.update_node(request.path_params["node_id"], await _json_body(request))


async def _admin_delete_node(request) -> dict:
    cascade = request.query_params.get("cascade", "").lower() in ("1", "true", "yes")
    return cmdb_admin.delete_node(request.path_params["node_id"], cascade=cascade)


async def _admin_create_edge(request) -> dict:
    return cmdb_admin.create_edge(await _json_body(request))


async def _admin_delete_edge(request) -> dict:
    return cmdb_admin.delete_edge(request.path_params["edge_id"])


def _admin_routes() -> list[Route]:
    """管理路由。**未启用时返回空列表**——路径根本不存在，比"存在但拒绝"更难绕过。"""
    if not get_settings().admin_enabled:
        return []
    logger.info("CMDB 管理端点已启用：/admin/cmdb/**")
    return [
        Route("/admin/cmdb/schema", _admin_route(_admin_schema), methods=["GET"]),
        Route("/admin/cmdb/summary", _admin_route(_admin_summary), methods=["GET"]),
        Route("/admin/cmdb/nodes", _admin_route(_admin_list_nodes), methods=["GET"]),
        Route("/admin/cmdb/nodes", _admin_route(_admin_create_node), methods=["POST"]),
        Route("/admin/cmdb/nodes/{node_id}", _admin_route(_admin_update_node), methods=["PUT"]),
        Route("/admin/cmdb/nodes/{node_id}", _admin_route(_admin_delete_node), methods=["DELETE"]),
        Route("/admin/cmdb/edges", _admin_route(_admin_create_edge), methods=["POST"]),
        Route("/admin/cmdb/edges/{edge_id}", _admin_route(_admin_delete_edge), methods=["DELETE"]),
    ]


def build_app(mcp=None) -> Starlette:
    """构建最终 ASGI 应用（含 /health、/admin/cmdb 与 MCP 端点）。"""
    server = mcp if mcp is not None else create_mcp_server()
    inner_app = server.streamable_http_app()

    routes = [
        Route("/health", _health, methods=["GET"]),
        *_admin_routes(),
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
