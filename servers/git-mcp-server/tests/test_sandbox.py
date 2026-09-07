"""路径沙箱测试（白名单 / fail-closed / 路径穿越 / 项目名解析）。"""
from __future__ import annotations

import pytest
from git_mcp_server.auth.sandbox import resolve_repo_ref, validate_repo_path
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


# ---------------------------------------------------------------- 项目名解析（resolve_repo_ref）
def test_absolute_path_still_resolves(env, git_repo, monkeypatch):
    """回归：绝对路径走原白名单校验。"""
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    assert resolve_repo_ref(str(git_repo)) == str(git_repo.resolve())


def test_name_matches_root_basename(env, git_repo, monkeypatch):
    """项目名 = allowed root 的 basename（root 本身是 git 仓库）。"""
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))  # basename "repo"
    assert resolve_repo_ref("repo") == str(git_repo.resolve())


def test_name_matches_child_dir(container_repo):
    """项目名 = allowed root 的一级子目录（container/"repo"）。"""
    resolved = resolve_repo_ref("repo")
    assert resolved == str((container_repo / "repo").resolve())


def test_name_resolves_in_second_root(two_roots):
    """多 root：项目名命中第二棵树的子目录（模拟 zjb + monorepo 两树）。"""
    root1, root2 = two_roots
    assert resolve_repo_ref("alpha") == str((root1 / "alpha").resolve())
    assert resolve_repo_ref("beta") == str((root2 / "beta").resolve())


def test_unknown_name_rejected(container_repo):
    with pytest.raises(AppError) as exc:
        resolve_repo_ref("no-such-proj")
    assert exc.value.code.name == "PERMISSION_DENIED"
    assert "no-such-proj" in exc.value.message


def test_tilde_in_root_and_input_expanded(env, tmp_path, monkeypatch):
    """~ 同时出现在 allowed root 与输入时均正确展开（expanduser 修复）。"""
    home = tmp_path / "fakehome"
    (home / "work" / "repo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "~/work/repo")
    assert validate_repo_path("~/work/repo") == str((home / "work" / "repo").resolve())


def test_symlinked_child_escaping_root_rejected(env, tmp_path, monkeypatch):
    """项目名命中 symlink 子目录指向 root 外 → 拒绝（fail-closed 对称）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "container2"
    root.mkdir()
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(root))
    with pytest.raises(AppError) as exc:
        resolve_repo_ref("escape")
    assert exc.value.code.name == "PERMISSION_DENIED"
