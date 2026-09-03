"""路径沙箱测试（白名单 / fail-closed / 路径穿越）。"""
from __future__ import annotations

import pytest
from git_mcp_server.auth.sandbox import validate_repo_path
from git_mcp_server.errors import AppError


def test_allowed_path_resolves(env, git_repo, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    resolved = validate_repo_path(str(git_repo))
    assert resolved == str(git_repo.resolve())


def test_allowed_subdirectory(env, git_repo, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    sub = git_repo / "sub"
    resolved = validate_repo_path(str(sub))
    assert resolved == str(sub.resolve())


def test_outside_allowed_root_rejected(env, git_repo, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with pytest.raises(AppError) as exc:
        validate_repo_path(str(git_repo.parent / "other"))
    assert exc.value.code.name == "PERMISSION_DENIED"


def test_fail_closed_without_roots(env):
    """未配置白名单 → 拒绝所有。"""
    with pytest.raises(AppError) as exc:
        validate_repo_path("/tmp")
    assert exc.value.code.name == "PERMISSION_DENIED"


def test_path_traversal_rejected(env, git_repo, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with pytest.raises(AppError) as exc:
        validate_repo_path(str(git_repo) + "/../evil")
    assert exc.value.code.name == "PATH_NOT_ALLOWED"


def test_symlink_is_resolved(env, git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    link = tmp_path / "link"
    link.symlink_to(git_repo)
    assert validate_repo_path(str(link)) == str(git_repo.resolve())
