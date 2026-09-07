"""共享 fixtures：环境变量（settings 缓存清理）、临时 git 仓库、ASGI 测试客户端。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def clear_settings_cache():
    """清理 get_settings 的 lru_cache，避免跨测试的 env 污染。"""
    from git_mcp_server.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch, clear_settings_cache):
    """基础环境变量（开发模式、无认证、允许临时根）。"""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "")
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "100")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("GIT_COMMAND_TIMEOUT_SEC", "30")
    monkeypatch.setenv("GIT_MAX_OUTPUT_BYTES", "1048576")
    yield


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _make_git_repo(path: Path) -> Path:
    """在给定 path 构建带两个提交 + feature 分支的临时 git 仓库。"""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=path)
    _git("config", "user.email", "t@test.local", cwd=path)
    _git("config", "user.name", "Tester", cwd=path)
    (path / "a.txt").write_text("hello world\nsecond line\n")
    (path / "sub").mkdir()
    (path / "sub" / "b.txt").write_text("alpha\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "first commit", cwd=path)
    # second commit: 修改 a.txt（用于 blame/status/log 断言）
    (path / "a.txt").write_text("hello world\nmodified line\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "second commit", cwd=path)
    _git("checkout", "-qb", "feature", cwd=path)
    (path / "a.txt").write_text("hello world\nmodified line\nfeature change\n")
    _git("commit", "-qam", "feature commit", cwd=path)
    _git("checkout", "-q", "main", cwd=path)
    return path


@pytest.fixture
def git_repo(tmp_path):
    """构建带两个提交的临时 git 仓库（目录名 "repo"）。"""
    return _make_git_repo(tmp_path / "repo")


@pytest.fixture
def allowed_env(env, git_repo, monkeypatch):
    """env + 允许仓库根指向临时 git 仓库。"""
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    yield git_repo


@pytest.fixture
def container_repo(env, tmp_path, monkeypatch):
    """容器 root：git 仓库是其一级子目录（root/"repo"）。用于「项目名=一级子目录」解析测试。"""
    root = tmp_path / "container"
    root.mkdir()
    _make_git_repo(root / "repo")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(root))
    yield root


@pytest.fixture
def two_roots(env, tmp_path, monkeypatch):
    """两个 allowed root，模拟「zjb 独立仓库群 + monorepo」两棵树：root1/"alpha"、root2/"beta"。"""
    root1 = tmp_path / "root1"
    root2 = tmp_path / "root2"
    _make_git_repo(root1 / "alpha")
    _make_git_repo(root2 / "beta")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", f"{root1},{root2}")
    yield root1, root2
