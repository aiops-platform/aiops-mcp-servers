"""6 个只读 Git 工具。

全部使用 porcelain/机器可读格式 + NUL/单元分隔符解析，避免文本解析脆弱性。
所有用户可控的路径/提交参数前均加 `--end-of-options`，防止选项注入。
"""
from __future__ import annotations

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from git_mcp_server.tools.base import require_repo, tool_guard
from git_mcp_server.tools.git.executor import GitExecutor

# --- 通用约束 ---
_MAX_LOG = 100
_MAX_BLAME_LINES = 500

StatusType = Annotated[
    str,
    Field(
        description=(
            "Git 仓库定位：绝对路径（须在 GIT_ALLOWED_ROOTS 内）或项目名"
            "（某 allowed root 的 basename 或其一级子目录名，如 multi-agent-workflow）。"
            "Absolute path within GIT_ALLOWED_ROOTS, or a project name "
            "(an allowed root's basename or its direct child directory name)."
        )
    ),
]

_STATUS_TEXT = {
    "??": "untracked",
    ".M": "modified_worktree",
    "M.": "modified_staged",
    "MM": "modified_staged_and_worktree",
    "A.": "added_to_index",
    "D.": "deleted_from_index",
    ".D": "deleted_worktree",
    "R.": "renamed",
    "C.": "copied",
    "UU": "unmerged",
}


def _describe_status(xy: str) -> str:
    return _STATUS_TEXT.get(xy, xy)


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


# ---- 1. status
def _build_status():
    @tool_guard("get_repo_status")
    def get_repo_status(
        repo_path: StatusType,
    ) -> dict:
        """查看仓库当前工作区状态（porcelain v2）。"""
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="get_repo_status")
        executor.ensure_git_repo()
        out = executor.run("status", "--porcelain=v2", "--branch")

        branch: dict = {"name": None, "oid": None, "upstream": None, "ahead": 0, "behind": 0}
        changed: list[dict] = []
        for line in out.splitlines():
            if line.startswith("# branch.oid "):
                branch["oid"] = line.split(" ", 2)[2]
            elif line.startswith("# branch.head "):
                branch["name"] = line.split(" ", 2)[2] or None
            elif line.startswith("# branch.upstream "):
                branch["upstream"] = line.split(" ", 2)[2] or None
            elif line.startswith("# branch.ab "):
                parts = line.split()
                if len(parts) >= 4:
                    branch["ahead"] = int(parts[2])
                    branch["behind"] = int(parts[3])
            elif line.startswith("? "):
                # 未跟踪文件：`? <path>`
                changed.append(
                    {
                        "path": line[2:],
                        "status": "??",
                        "status_text": _describe_status("??"),
                    }
                )
            elif line.startswith(("1 ", "2 ", "u ")):
                # porcelain v2：1/u 类为空格分隔（path 为最后一个 token），
                # 2 类（重命名）用 tab 分隔原路径。
                fields = line.split("\t")
                tokens = fields[0].split()
                xy = tokens[1] if len(tokens) > 1 else "??"
                entry: dict = {
                    "path": tokens[-1],
                    "status": xy,
                    "status_text": _describe_status(xy),
                }
                if fields[0].startswith("2 ") and len(fields) > 1:
                    entry["original_path"] = fields[1]
                changed.append(entry)

        return {
            "branch": branch,
            "changed_files": changed,
            "changed_count": len(changed),
            "clean": len(changed) == 0,
        }

    return get_repo_status


# ---- 2. commit log
def _build_log():
    @tool_guard("get_commit_log")
    def get_commit_log(
        repo_path: StatusType,
        max_count: Annotated[
            int, Field(ge=1, le=_MAX_LOG, description="Maximum number of commits (1-100)")
        ] = 10,
    ) -> dict:
        """获取分支提交历史（最多 100 条）。"""
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="get_commit_log")
        executor.ensure_git_repo()
        fmt = "%H%x1f%an%x1f%ae%x1f%ad%x1f%s"
        out = executor.run(
            "log", "-n", str(max_count),
            f"--pretty=tformat:{fmt}", "--date=iso-strict",
            allow_exit_codes=(128,),  # 空仓库（无任何提交）
        )
        commits: list[dict] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 5 and parts[0]:
                commits.append(
                    {
                        "sha": parts[0],
                        "author_name": parts[1],
                        "author_email": parts[2],
                        "author_date": parts[3],
                        "subject": parts[4],
                    }
                )
        return {"commits": commits, "count": len(commits)}

    return get_commit_log


# ---- 3. commit detail
def _build_detail():
    @tool_guard("get_commit_detail")
    def get_commit_detail(
        repo_path: StatusType,
        commit_sha: Annotated[
            str,
            Field(min_length=4, max_length=40, description="Commit sha or ref (e.g. HEAD, abc123)"),
        ],
    ) -> dict:
        """获取单个提交的元数据与变更统计。"""
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="get_commit_detail")
        executor.ensure_git_repo()
        fmt = "%H%x1f%P%x1f%an%x1f%ae%x1f%ad%x1f%cn%x1f%ce%x1f%cd%x1f%s%x1f%b"
        out = executor.run(
            "show",
            "-s",
            f"--pretty=format:{fmt}",
            "--date=iso-strict",
            "--end-of-options",
            commit_sha,
        )
        parts = out.split("\x1f")
        if len(parts) < 10 or not parts[0]:
            return {"sha": commit_sha, "found": False}
        stat_out = executor.run("show", "--format=", "--stat", "--end-of-options", commit_sha)
        meta = {
            "sha": parts[0],
            "parents": [p for p in parts[1].split() if p],
            "author_name": parts[2],
            "author_email": parts[3],
            "author_date": parts[4],
            "committer_name": parts[5],
            "committer_email": parts[6],
            "committer_date": parts[7],
            "subject": parts[8],
            "body": parts[9].strip(),
            "found": True,
        }
        stat_lines = [ln.strip() for ln in stat_out.splitlines() if ln.strip()]
        meta["stat"] = stat_lines
        return meta

    return get_commit_detail


# ---- 4. branches
def _build_branches():
    @tool_guard("list_branches")
    def list_branches(
        repo_path: StatusType,
    ) -> dict:
        """列出本地与远程跟踪分支。"""
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="list_branches")
        executor.ensure_git_repo()
        # for-each-ref 不支持 %x1f 转义，使用制表符分隔（refname 不含制表符）
        fmt = "%(refname:short)\t%(objectname:short)\t%(HEAD)\t%(upstream:short)"
        out = executor.run("for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes")
        branches: list[dict] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            name = fields[0]
            short_sha = fields[1] if len(fields) > 1 else ""
            head = fields[2] if len(fields) > 2 else ""
            upstream = fields[3] if len(fields) > 3 else ""
            branches.append(
                {
                    "name": name,
                    "short_sha": short_sha,
                    "is_current": head == "*",
                    "upstream": upstream or None,
                }
            )
        return {"branches": branches, "count": len(branches)}

    return list_branches


# ---- 5. grep
def _build_search():
    @tool_guard("search_code")
    def search_code(
        repo_path: StatusType,
        query: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Text pattern to search — POSIX **ERE** (`git grep -E`): "
                    "`a|b` IS alternation, `.` matches any char, `\\b`/`\\s` work as usual"
                ),
            ),
        ],
        path: Annotated[
            str, Field(description="Optional sub-path to restrict search within")
        ] = "",
        case_sensitive: Annotated[bool, Field(description="Case-sensitive match")] = False,
    ) -> dict:
        """在受跟踪文件中全文搜索（``git grep -E``），返回 file:line:column:content。

        ⚠️ ``-E`` 不是口味问题：**默认的 BRE 会把 ``|`` 当普通字符**，于是调用方最常写的
        ``print|export|Print|Export`` 这种交替查询返回 0 条且**不报错**（静默吞掉）——
        它据此以为"仓库里没有"，接着换关键词，把轮数耗光。实测 2026-09-23（run_a5ab9555a3）：
        ``code-locator`` 的三条交替查询全部静默 0 条（其中一条还因括号不平衡 exit 128），
        而它第 8 步读到的文件**已经是对的**——只因提示词要求"以 search_code 的返回为准"，
        它不敢收口，最终 10 轮耗尽 → ``locate`` 判负 → ``halt`` → 整条诊断中断。
        同仓验过：BRE 空、加 ``-E`` 命中 29 条。别退回去。
        """
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="search_code")
        executor.ensure_git_repo()
        args = ["grep", "-n", "--column", "-E"]
        if not case_sensitive:
            args.append("-i")
        args.append("--end-of-options")
        args.append(query)
        args.append("--")
        if path:
            args.append(path)
        out = executor.run(*args, allow_exit_codes=(1,))  # exit 1 = 无匹配
        matches: list[dict] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            file_, rest = line.split(":", 1)
            line_no, rest = rest.split(":", 1)
            col, content = rest.split(":", 1)
            matches.append(
                {"file": file_, "line": int(line_no), "column": int(col), "content": content}
            )
        return {"matches": matches, "count": len(matches), "truncated": False}

    return search_code


# ---- 6. blame
def _build_blame():
    @tool_guard("blame_file")
    def blame_file(
        repo_path: StatusType,
        file_path: Annotated[str, Field(description="Tracked file path within the repo")],
        start_line: Annotated[int, Field(ge=1, description="Start line (1-based)")] = 1,
        end_line: Annotated[int, Field(ge=1, description="End line (inclusive)")] = 20,
    ) -> dict:
        """按行追溯文件，返回每行的提交与作者（--line-porcelain）。"""
        repo = require_repo(repo_path)
        executor = GitExecutor(repo, tool="blame_file")
        executor.ensure_git_repo()
        start = max(1, int(start_line))
        end = max(start, int(end_line))
        if end - start + 1 > _MAX_BLAME_LINES:
            end = start + _MAX_BLAME_LINES - 1
        out = executor.run(
            "blame", "--line-porcelain", "-L", f"{start},{end}", "--", file_path
        )
        lines = out.splitlines()
        result: list[dict] = []
        current: dict | None = None
        for line in lines:
            if line.startswith("\t"):
                if current is not None:
                    current["content"] = line[1:]
                    result.append(current)
                    current = None
                continue
            head = line.split(" ")
            if len(head) >= 3 and _looks_like_sha(head[0]):
                current = {"sha": head[0], "orig_line": int(head[1]), "final_line": int(head[2])}
                continue
            if current is None:
                continue
            if line.startswith("author "):
                current["author"] = line[len("author "):]
            elif line.startswith("author-mail "):
                current["author_mail"] = line[len("author-mail "):]
            elif line.startswith("author-time "):
                current["author_time"] = line[len("author-time "):]
            elif line.startswith("summary "):
                current["summary"] = line[len("summary "):]
            elif line.startswith("filename "):
                current["filename"] = line[len("filename "):]
        return {
            "lines": result,
            "count": len(result),
            "start_line": start,
            "end_line": start + len(result) - 1,
        }

    return blame_file


# ---- registry
_FACTORIES = {
    "get_repo_status": _build_status,
    "get_commit_log": _build_log,
    "get_commit_detail": _build_detail,
    "list_branches": _build_branches,
    "search_code": _build_search,
    "blame_file": _build_blame,
}


def register_tools(server, enabled: set[str]) -> None:
    """将启用集合内的工具注册到 FastMCP server。

    6 个 git 工具都只跑只读命令（porcelain/机器可读格式 + 沙箱路径白名单），故统一声明
    ``readOnlyHint=True`` —— AgentScope 据此把工具判为只读并自动 ALLOW（§9.5），
    agentflow MCP 配置页的 ``read_only`` / 「只读」徽章即来自该标注。
    """
    for name, factory in _FACTORIES.items():
        if name in enabled:
            server.tool(
                name=name,
                annotations=ToolAnnotations(readOnlyHint=True),
            )(factory())
