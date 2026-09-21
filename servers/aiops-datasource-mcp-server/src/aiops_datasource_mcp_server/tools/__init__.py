"""工具注册：把各模块的工厂产出注册到 FastMCP。

**只读与写面分开注册**——`readOnlyHint` 不是装饰性元数据：agent 侧（AgentScope）
据此自动 ALLOW 工具。把一个写工具标成只读，等于让它**无需任何授权**就能产生外部
副作用。所以这里按模块显式指定，不写死一个常量。

- `datasource.FACTORIES` → `readOnlyHint=True`（全部只读查询）
- `ticket.FACTORIES`     → `readOnlyHint=False`（工单回传，会真的改别人的工单）
"""
from __future__ import annotations

import logging

from mcp.types import ToolAnnotations

from aiops_datasource_mcp_server.tools.datasource import FACTORIES as READ_ONLY_FACTORIES
from aiops_datasource_mcp_server.tools.ticket import FACTORIES as WRITE_FACTORIES

logger = logging.getLogger("aiops_datasource_mcp_server.tools")


def register(server) -> list[str]:
    """注册全部工具；返回已注册工具名列表。"""
    names: list[str] = []
    for read_only, factories in ((True, READ_ONLY_FACTORIES), (False, WRITE_FACTORIES)):
        for name, factory in factories.items():
            server.tool(name=name, annotations=ToolAnnotations(readOnlyHint=read_only))(factory())
            names.append(name)
            logger.info("registered tool %s (read_only=%s)", name, read_only)
    return names
