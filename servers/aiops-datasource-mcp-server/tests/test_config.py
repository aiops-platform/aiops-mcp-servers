"""配置：默认值、环境校验、端口一致性。"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.config import Settings, get_settings


def test_defaults(clear_settings_cache) -> None:
    s = Settings(_env_file=None)
    assert s.bind_port == 8300          # git=8000/8100、applog=8200/8101 之后
    assert s.datasource_es_index == "app-logs"
    assert s.datasource_max_range_hours == 24.0
    assert s.auth_token == ""           # 默认不启用认证（仅开发）
    assert s.datasource_kubectl_bin == "kubectl"


def test_env_prefix_is_datasource(monkeypatch, clear_settings_cache) -> None:
    """领域变量统一 DATASOURCE_ 前缀（对齐 applog 的 APPLOG_ 约定）。"""
    monkeypatch.setenv("DATASOURCE_ES_URL", "http://es.example:9200")
    monkeypatch.setenv("DATASOURCE_K8S_NAMESPACE", "prod")
    s = Settings(_env_file=None)
    assert s.datasource_es_url == "http://es.example:9200"
    assert s.datasource_k8s_namespace == "prod"


def test_production_requires_auth_token(monkeypatch, clear_settings_cache) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "")
    s = Settings(_env_file=None)
    with pytest.raises(ValueError, match="AUTH_TOKEN"):
        s.validate_for_environment()


def test_production_with_token_passes(monkeypatch, clear_settings_cache) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    Settings(_env_file=None).validate_for_environment()  # 不抛即通过


def test_development_without_token_ok(monkeypatch, clear_settings_cache) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    Settings(_env_file=None).validate_for_environment()


def test_positive_constraints_reject_zero(clear_settings_cache) -> None:
    """超时/字节上限/跨度上限必须为正——0 会让查询静默失败或无限。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, datasource_request_timeout_sec=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, datasource_max_range_hours=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, datasource_max_response_bytes=0)


def test_settings_cached(clear_settings_cache) -> None:
    assert get_settings() is get_settings()
