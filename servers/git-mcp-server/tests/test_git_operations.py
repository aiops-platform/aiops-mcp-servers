"""6 个只读 Git 工具的行为测试。"""
from __future__ import annotations

import pytest
from git_mcp_server.errors import AppError
from git_mcp_server.tools.git.operations import (
    _build_blame,
    _build_branches,
    _build_detail,
    _build_log,
    _build_search,
    _build_status,
)


def _repo_path(allowed_env):
    return str(allowed_env)


# ---------------------------------------------------------------- status
def test_status_clean(allowed_env):
    out = _build_status()(repo_path=_repo_path(allowed_env))
    assert out["clean"] is True
    assert out["changed_count"] == 0
    assert out["branch"]["name"] == "main"


def test_status_detects_modified(allowed_env):
    (allowed_env / "a.txt").write_text("dirty\n")
    out = _build_status()(repo_path=_repo_path(allowed_env))
    assert out["clean"] is False
    changed = out["changed_files"][0]
    assert changed["path"] == "a.txt"
    assert changed["status"] in {".M", "MM"}


def test_status_reports_untracked(allowed_env):
    (allowed_env / "untracked.txt").write_text("x\n")
    out = _build_status()(repo_path=_repo_path(allowed_env))
    statuses = {c["status"] for c in out["changed_files"]}
    assert "??" in statuses


# ---------------------------------------------------------------- log
def test_log_returns_commits(allowed_env):
    out = _build_log()(repo_path=_repo_path(allowed_env), max_count=10)
    assert out["count"] == 2
    shas = [c["sha"] for c in out["commits"]]
    assert len(set(shas)) == 2
    assert out["commits"][0]["subject"] == "second commit"


def test_log_max_count_bounds(allowed_env):
    out = _build_log()(repo_path=_repo_path(allowed_env), max_count=1)
    assert out["count"] == 1
    assert out["commits"][0]["subject"] == "second commit"


# ---------------------------------------------------------------- detail
def test_commit_detail(allowed_env):
    out = _build_detail()(repo_path=_repo_path(allowed_env), commit_sha="HEAD")
    assert out["found"] is True
    assert out["subject"] == "second commit"
    assert len(out["parents"]) == 1
    assert out["stat"]  # 非空 diffstat


def test_commit_detail_bad_sha(allowed_env):
    with pytest.raises(AppError):
        _build_detail()(repo_path=_repo_path(allowed_env), commit_sha="deadbeefdeadbeef")


# ---------------------------------------------------------------- branches
def test_list_branches(allowed_env):
    out = _build_branches()(repo_path=_repo_path(allowed_env))
    names = {b["name"] for b in out["branches"]}
    assert {"main", "feature"} <= names
    current = [b for b in out["branches"] if b["is_current"]]
    assert current and current[0]["name"] == "main"


# ---------------------------------------------------------------- search
def test_search_code_finds_match(allowed_env):
    out = _build_search()(repo_path=_repo_path(allowed_env), query="hello")
    assert out["count"] == 1
    m = out["matches"][0]
    assert m["file"] == "a.txt"
    assert m["line"] == 1
    assert "hello" in m["content"]


def test_search_code_case_insensitive(allowed_env):
    search = _build_search()
    assert search(repo_path=_repo_path(allowed_env), query="HELLO")["count"] == 1
    assert (
        search(repo_path=_repo_path(allowed_env), query="HELLO", case_sensitive=True)["count"]
        == 0
    )


def test_search_code_no_match_returns_empty(allowed_env):
    out = _build_search()(repo_path=_repo_path(allowed_env), query="zzzznomatch")
    assert out["count"] == 0


def test_search_code_query_is_ere_so_alternation_works(allowed_env):
    """查询按 **POSIX ERE** 解释：``a|b`` 是交替，不是字面量。

    这条测的是一个**静默**失效模式，不是"能不能搜到"：``git grep`` 默认走 BRE，
    ``|`` 在那里是普通字符，于是 ``hello|alpha`` 会去找字面量 ``hello|alpha``
    —— **0 条命中、也不报错**。调用方（``code-locator``）据此认定"仓库里没有"，
    换关键词再来，把轮数耗光 → ``locate`` 判负 → ``halt`` → 整条诊断中断。

    实测（2026-09-23，run_a5ab9555a3）：三条交替查询
    （``print|export|Print|Export``、``indexOf|substring|charAt|…``（这条还因括号不平衡
    直接 exit 128）、``generateQuotation|renderItems|composeQuotation``）全部返回 0 条，
    而同仓 ``git grep -E`` 立刻有命中。所以断言刻意取"两个文件都命中"：
    若退回 BRE，这条会以 count == 0 失败（而不是碰巧仍为 2）。
    """
    out = _build_search()(repo_path=_repo_path(allowed_env), query="hello|alpha")
    assert out["count"] == 2, out
    assert {m["file"] for m in out["matches"]} == {"a.txt", "sub/b.txt"}


# ---------------------------------------------------------------- blame
def test_blame_file(allowed_env):
    out = _build_blame()(
        repo_path=_repo_path(allowed_env), file_path="a.txt", start_line=1, end_line=2
    )
    assert out["count"] == 2
    assert out["lines"][0]["content"] == "hello world"
    assert out["lines"][1]["author"] == "Tester"


def test_blame_bounds_are_capped(allowed_env):
    out = _build_blame()(
        repo_path=_repo_path(allowed_env), file_path="a.txt", start_line=1, end_line=500
    )
    assert out["count"] <= 500


# ---------------------------------------------------------------- guards
def test_repo_must_be_git_repo(env, git_repo, tmp_path, monkeypatch):
    # 允许根同时包含 git_repo 与一个普通目录，普通目录不是 git 仓库
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", f"{git_repo},{plain}")
    with pytest.raises(AppError) as exc:
        _build_status()(repo_path=str(plain))
    assert exc.value.code.name == "INVALID_REQUEST"


def test_path_outside_roots_blocked(env, git_repo, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    outside = git_repo.parent / "outside"
    outside.mkdir()
    with pytest.raises(AppError) as exc:
        _build_log()(repo_path=str(outside))
    assert exc.value.code.name == "PERMISSION_DENIED"


# ---------------------------------------------------------------- 项目名（resolve_repo_ref）全链
def test_tool_accepts_child_project_name(container_repo):
    """repo_path 用一级子目录名 → 工具全链命中。"""
    out = _build_status()(repo_path="repo")
    assert out["clean"] is True
    assert out["branch"]["name"] == "main"


def test_tool_accepts_root_basename(allowed_env):
    """repo_path 用 allowed root 的 basename（root 本身即 git 仓库）。"""
    out = _build_log()(repo_path="repo", max_count=10)  # git_repo basename = "repo"
    assert out["count"] == 2


def test_tool_accepts_project_from_second_root(two_roots):
    """多 root：repo_path 用第二棵树的一级子目录名。"""
    out = _build_status()(repo_path="beta")
    assert out["clean"] is True
    assert out["branch"]["name"] == "main"
