"""工具注册：读 config/tools.yaml → 逐个 build → 注册到 FastMCP。

每个工具统一标注 readOnlyHint=True（查询类只读，agent 侧据此自动 ALLOW）。
"""
from __future__ import annotations

import logging

from mcp.types import ToolAnnotations

from applog_mcp_server.tools.factory import build_tool
from applog_mcp_server.tools.loader import load_tool_specs

logger = logging.getLogger("applog_mcp_server.tools")


def register(server) -> list[str]:
    """读取 tools.yaml 并注册全部工具；返回已注册工具名列表。"""
    specs = load_tool_specs()
    names: list[str] = []
    for spec in specs:
        fn = build_tool(spec)
        server.tool(
            name=spec.name,
            description=spec.description,
            annotations=ToolAnnotations(readOnlyHint=True),
        )(fn)
        names.append(spec.name)
        logger.info(
            "registered tool %s (method=%s %s)", spec.name, spec.http.method, spec.http.path
        )
    return names
