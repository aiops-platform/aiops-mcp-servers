"""GitExecutor — 以 subprocess + list 参数安全执行只读 git 命令。

安全设计（替代 GitPython）：
- list 参数（无 shell）：从根上消除 shell 注入。
- 显式 timeout：防止命令挂死（默认 30s）。
- 安全环境：GIT_TERMINAL_PROMPT=0（禁交互认证）、GIT_PAGER=cat（禁分页器）、LC_ALL=C。
- 输出字节上限 + UTF-8 边界截断：防止大输出撑爆内存，并在截断处标注。
- 工作目录固定于仓库根（git -C <repo>），配合沙箱校验的 realpath。
"""
from __future__ import annotations

import os
import subprocess
import time

from git_mcp_server.config import get_settings
from git_mcp_server.errors import AppError, ErrorCode
from git_mcp_server.metrics import GIT_COMMAND_DURATION, GIT_COMMAND_TOTAL


class GitExecutor:
    def __init__(self, repo_path: str, tool: str = "git"):
        self.repo_path = repo_path
        self.tool = tool
        self.settings = get_settings()
        self._env = os.environ.copy()
        self._env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",  # 禁交互认证，防止挂起/凭据窃取
                "GIT_PAGER": "cat",  # 禁用分页器，避免命令挂起
                "PAGER": "cat",
                "LC_ALL": "C",  # 稳定输出语言，便于解析
                "LANG": "C",
            }
        )

    def run(self, *args: str, allow_exit_codes: tuple[int, ...] = ()) -> str:
        """执行 git 命令，返回解码后的标准输出（已截断到字节上限）。

        allow_exit_codes: 允许视为"正常"的非零退出码（如 git grep 无匹配返回 1）。
        """
        command = ["git", "-C", self.repo_path, *args]
        timeout = self.settings.git_command_timeout_sec

        GIT_COMMAND_TOTAL.labels(tool=self.tool).inc()
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(
                ErrorCode.TOOL_EXECUTION_ERROR,
                f"git command timed out after {timeout:.1f}s",
                {"command": " ".join(command)},
            ) from exc
        except OSError as exc:
            raise AppError(
                ErrorCode.TOOL_EXECUTION_ERROR,
                f"failed to start git: {exc}",
            ) from exc
        finally:
            GIT_COMMAND_DURATION.labels(tool=self.tool).observe(time.perf_counter() - start)

        if proc.returncode != 0 and proc.returncode not in allow_exit_codes:
            stderr = self._decode(proc.stderr)
            raise AppError(
                ErrorCode.TOOL_EXECUTION_ERROR,
                f"git command failed (exit {proc.returncode}): {stderr.strip()}",
                {"command": " ".join(command)},
            )

        return self._decode(proc.stdout)

    def ensure_git_repo(self) -> None:
        """校验 repo_path 确实是一个 git 仓库（否则抛出明确的业务错误）。"""
        try:
            self.run("rev-parse", "--git-dir")
        except AppError as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"'{self.repo_path}' is not a git repository",
            ) from exc

    def _decode(self, data: bytes) -> str:
        """按字节上限截断（在 UTF-8 边界处安全截断），避免解码异常。"""
        if len(data) > self.settings.git_max_output_bytes:
            data = data[: self.settings.git_max_output_bytes]
            truncated = True
        else:
            truncated = False

        # 从末尾回退，确保在 UTF-8 字符边界处截断
        cut = len(data)
        while cut > 0 and (data[cut - 1] & 0xC0) == 0x80:  # 0x80~0xBF 为连续字节
            cut -= 1
        text = data[:cut].decode("utf-8", errors="replace")
        if truncated:
            text += "\n... [truncated]"
        return text
