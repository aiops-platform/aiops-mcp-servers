"""工具注册：把各模块的工厂产出注册到 FastMCP。

**只读与写面分开注册**——`readOnlyHint` 不是装饰性元数据：agent 侧（AgentScope）
据此自动 ALLOW 工具。把一个写工具标成只读，等于让它**无需任何授权**就能产生外部副作用。

⚠️ 本 server 的写面尤其如此：`rollout_deployment` 会**改线上**。它标成
`readOnlyHint=False` 之后，agentflow 侧才会走需要授权的路径。

- `deploy.READ_ONLY_FACTORIES` → `readOnlyHint=True`
- `deploy.WRITE_FACTORIES`     → `readOnlyHint=False`
"""
from __future__ import annotations

import logging

from mcp.types import ToolAnnotations

from deploy_ops_mcp_server.tools.deploy import READ_ONLY_FACTORIES, WRITE_FACTORIES

logger = logging.getLogger("deploy_ops_mcp_server.tools")


def register(server) -> list[str]:
    """注册全部工具；返回已注册工具名列表。"""
    names: list[str] = []
    for read_only, factories in ((True, READ_ONLY_FACTORIES), (False, WRITE_FACTORIES)):
        for name, factory in factories.items():
            server.tool(name=name, annotations=ToolAnnotations(readOnlyHint=read_only))(factory())
            names.append(name)
            logger.info("registered tool %s (read_only=%s)", name, read_only)
    return names
