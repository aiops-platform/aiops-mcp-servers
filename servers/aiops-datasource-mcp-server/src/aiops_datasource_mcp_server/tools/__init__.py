"""工具注册：把 datasource.py 的工厂产出注册到 FastMCP。

每个工具统一标注 ``readOnlyHint=True``——agent 侧（AgentScope）据此自动 ALLOW，
无需额外配置写死工具名。
"""
from __future__ import annotations

import logging

from mcp.types import ToolAnnotations

from aiops_datasource_mcp_server.tools.datasource import FACTORIES

logger = logging.getLogger("aiops_datasource_mcp_server.tools")


def register(server) -> list[str]:
    """注册全部工具；返回已注册工具名列表。"""
    names: list[str] = []
    for name, factory in FACTORIES.items():
        fn = factory()
        server.tool(
            name=name,
            annotations=ToolAnnotations(readOnlyHint=True),
        )(fn)
        names.append(name)
        logger.info("registered tool %s", name)
    return names
