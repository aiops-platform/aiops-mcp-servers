"""工具注册：TOOLS_ENABLED 动态开关（逗号分隔，为空则全部启用）。"""
from __future__ import annotations

from git_mcp_server.config import get_settings
from git_mcp_server.tools.git.operations import register_tools

_ALL_TOOLS = {
    "get_repo_status",
    "get_commit_log",
    "get_commit_detail",
    "list_branches",
    "search_code",
    "blame_file",
}


def active_tools(enabled_csv: str | None = None) -> set[str]:
    """根据 TOOLS_ENABLED 过滤出启用的工具名集合。"""
    settings = get_settings()
    enabled = settings.tools_enabled_list if enabled_csv is None else [
        s.strip() for s in enabled_csv.split(",") if s.strip()
    ]
    if not enabled:
        return set(_ALL_TOOLS)
    unknown = set(enabled) - _ALL_TOOLS
    if unknown:
        raise ValueError(f"Unknown tools in TOOLS_ENABLED: {sorted(unknown)}")
    return set(enabled)


def register(server, tools: set[str] | None = None) -> None:
    """向 FastMCP 实例注册启用工具。"""
    enabled = active_tools() if tools is None else tools
    register_tools(server, enabled)
