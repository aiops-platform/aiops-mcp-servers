"""路径沙箱 — 白名单模式，fail-closed。

`repo_path` 支持两种定位方式（resolve_repo_ref）：
1. 绝对路径（GIT_ALLOWED_ROOTS 内）→ validate_repo_path 白名单校验；
2. 项目名（某 allowed root 的 basename 或其一级子目录名）→ name 解析到真实目录。
"""
import os

from git_mcp_server.config import get_settings
from git_mcp_server.errors import AppError, ErrorCode


def _looks_like_path(value: str) -> bool:
    """判定值像路径还是裸项目名：以 / ~ ./ ../ 开头，或含路径分隔符 → 路径。"""
    return (
        value.startswith(("/", "~", "./", "../"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _real_roots() -> list[str]:
    """allowed roots 统一 expanduser + realpath + 去重；空 → fail-closed。

    注意：os.path.realpath 不展开 "~"，必须先 expanduser（现网 .env 用 ~/ 时缺此
    会解析成 <cwd>/~/... 乱路径）。
    """
    settings = get_settings()
    raw = settings.allowed_roots_list
    if not raw:
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            "No allowed roots configured. Set GIT_ALLOWED_ROOTS.",
        )
    out: list[str] = []
    for r in raw:
        rr = os.path.realpath(os.path.expanduser(r))
        if rr not in out:
            out.append(rr)
    return out


def _is_under(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def validate_repo_path(repo_path: str) -> str:
    """验证绝对路径在允许的白名单内，返回解析后的真实路径。

    规则：
    1. 展开 ~ 后解析符号链接（expanduser + realpath）
    2. 白名单前缀匹配
    3. 白名单未配置 → 拒绝所有（fail-closed）
    4. 禁止 ".."（纵深防御）
    """
    if ".." in repo_path:
        raise AppError(
            ErrorCode.PATH_NOT_ALLOWED,
            "Path traversal detected: '..' is forbidden",
        )

    real_path = os.path.realpath(os.path.expanduser(repo_path))

    for root in _real_roots():
        if _is_under(real_path, root):
            return real_path

    raise AppError(
        ErrorCode.PERMISSION_DENIED,
        f"Path '{repo_path}' is not in allowed roots",
        {"allowed_roots": get_settings().allowed_roots_list, "resolved_path": real_path},
    )


def resolve_repo_ref(ref: str) -> str:
    """校验并解析 repo_path 参数：绝对路径 或 项目名，返回真实绝对路径。

    项目名匹配（跨全部 allowed root，顺序即优先级）：
    1. 等于某 root 的 basename → 该 root 本身；
    2. 等于某 root 的一级子目录名（root/<name> 存在）→ 该子目录（realpath 后须仍落在该 root 内，
       防 symlink 越界）；两遍都未命中 → PERMISSION_DENIED。
    """
    if _looks_like_path(ref):
        return validate_repo_path(ref)

    roots = _real_roots()  # 空 roots 在此抛 fail-closed
    for root in roots:  # 第一遍：root 名
        if ref == os.path.basename(root.rstrip(os.sep)):
            return root
    for root in roots:  # 第二遍：一级子目录名
        child = os.path.join(root, ref)
        real_child = os.path.realpath(child)
        if os.path.isdir(real_child) and _is_under(real_child, root):
            return real_child
    raise AppError(
        ErrorCode.PERMISSION_DENIED,
        f"未知项目名 '{ref}'：可用 = allowed root 名或其一级子目录名",
        {"allowed_roots": roots},
    )
