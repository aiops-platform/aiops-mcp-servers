"""路径沙箱 — 白名单模式，fail-closed。"""
import os

from git_mcp_server.config import get_settings
from git_mcp_server.errors import AppError, ErrorCode


def validate_repo_path(repo_path: str) -> str:
    """验证仓库路径在允许的白名单内，返回解析后的真实路径。

    规则：
    1. 解析符号链接（realpath）
    2. 白名单前缀匹配
    3. 白名单未配置 → 拒绝所有（fail-closed）
    4. 禁止 ".."（纵深防御）
    """
    settings = get_settings()

    if ".." in repo_path:
        raise AppError(
            ErrorCode.PATH_NOT_ALLOWED,
            "Path traversal detected: '..' is forbidden",
        )

    real_path = os.path.realpath(repo_path)

    allowed_roots = settings.allowed_roots_list
    if not allowed_roots:
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            "No allowed roots configured. Set GIT_ALLOWED_ROOTS.",
        )

    for root in allowed_roots:
        root = os.path.realpath(root)
        if real_path == root or real_path.startswith(root + os.sep):
            return real_path

    raise AppError(
        ErrorCode.PERMISSION_DENIED,
        f"Path '{repo_path}' is not in allowed roots",
        {"allowed_roots": allowed_roots, "resolved_path": real_path},
    )
