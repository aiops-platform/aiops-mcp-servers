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
