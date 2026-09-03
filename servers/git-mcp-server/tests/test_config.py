"""配置（pydantic-settings）测试。"""
from __future__ import annotations

import pytest
from git_mcp_server.config import Settings


def test_defaults(monkeypatch):
    """纯净默认值（不依赖 env fixture）。"""
    for key in (
        "ENVIRONMENT",
        "AUTH_TOKEN",
        "GIT_ALLOWED_ROOTS",
        "ALLOWED_HOSTS",
        "ALLOWED_ORIGINS",
        "TOOLS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.environment == "development"
    assert s.bind_host == "127.0.0.1"
    assert s.bind_port == 8000
    assert s.auth_token == ""
    assert s.git_command_timeout_sec == 30.0
    assert s.git_max_output_bytes == 1024 * 1024
    assert s.allowed_hosts == "localhost,127.0.0.1,::1"


def test_csv_helper():
    s = Settings(_env_file=None)
    assert s._csv("") == []
    assert s._csv("a,b , c") == ["a", "b", "c"]


def test_allowed_roots_list_from_csv(env, monkeypatch):
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "/a,/b , /c")
    s = Settings()
    assert s.allowed_roots_list == ["/a", "/b", "/c"]


def test_production_requires_auth(env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "/tmp")
    with pytest.raises(ValueError, match="AUTH_TOKEN"):
        Settings().validate_for_environment()


def test_production_requires_allowed_roots(env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "")
    with pytest.raises(ValueError, match="GIT_ALLOWED_ROOTS"):
        Settings().validate_for_environment()


def test_production_ok(env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "/tmp")
    Settings().validate_for_environment()  # 不应抛异常


def test_tools_enabled_list(env, monkeypatch):
    monkeypatch.setenv("TOOLS_ENABLED", "get_repo_status, search_code")
    assert Settings().tools_enabled_list == ["get_repo_status", "search_code"]
