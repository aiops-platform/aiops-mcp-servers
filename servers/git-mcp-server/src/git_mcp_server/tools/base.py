"""工具装饰器：路径沙箱校验 + 指标 + 异常统一出口。

FastMCP 会基于函数签名生成 JSON Schema，使用 functools.wraps 保持原签名可见。
"""
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from git_mcp_server.auth.sandbox import resolve_repo_ref
from git_mcp_server.errors import AppError, ErrorCode
from git_mcp_server.metrics import observe_request

logger = logging.getLogger("git_mcp_server.tools")


def tool_guard(tool_name: str) -> Callable:
    """装饰工具函数：计时指标、异常记录并透传 AppError。"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            timer = observe_request(tool_name)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                timer.stop(error=True)
                if isinstance(exc, AppError):
                    raise
                logger.exception("tool %s failed", tool_name)
                raise AppError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message=f"internal error in {tool_name}",
                ) from exc
            else:
                timer.stop()
                return result

        return wrapper

    return decorator


def require_repo(repo_path: str) -> str:
    """校验并解析仓库定位：绝对路径（白名单）或项目名（root 名 / root 一级子目录名）。"""
    return resolve_repo_ref(repo_path)
