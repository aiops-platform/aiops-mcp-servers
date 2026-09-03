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


@pytest.fixture
def git_repo(tmp_path):
    """构建带两个提交的临时 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=repo)
    _git("config", "user.email", "t@test.local", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    (repo / "a.txt").write_text("hello world\nsecond line\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("alpha\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "first commit", cwd=repo)
    # second commit: 修改 a.txt（用于 blame/status/log 断言）
    (repo / "a.txt").write_text("hello world\nmodified line\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "second commit", cwd=repo)
    _git("checkout", "-qb", "feature", cwd=repo)
    (repo / "a.txt").write_text("hello world\nmodified line\nfeature change\n")
    _git("commit", "-qam", "feature commit", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    return repo


@pytest.fixture
def allowed_env(env, git_repo, monkeypatch):
    """env + 允许仓库根指向临时 git 仓库。"""
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    yield git_repo
