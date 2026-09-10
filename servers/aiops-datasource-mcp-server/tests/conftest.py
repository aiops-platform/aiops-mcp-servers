"""共享 fixtures：清配置缓存 + 隔离常用 env + 上游默认地址。"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.config import get_settings


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch, clear_settings_cache):
    """隔离 Settings 常用项，指向测试用的上游地址。"""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("DATASOURCE_ES_URL", "http://es.test")
    monkeypatch.setenv("DATASOURCE_ES_INDEX", "app-logs")
    monkeypatch.setenv("DATASOURCE_PROM_URL", "http://prom.test")
    monkeypatch.setenv("DATASOURCE_K8S_NAMESPACE", "order")
    monkeypatch.setenv("DATASOURCE_REQUEST_TIMEOUT_SEC", "5.0")
    monkeypatch.setenv("DATASOURCE_MAX_RESPONSE_BYTES", "4096")
    monkeypatch.setenv("DATASOURCE_MAX_RANGE_HOURS", "24")
    return get_settings()


# 固定测试窗口（避免用 now() 造成断言不稳定）
START = "2026-09-10T06:00:00"
END = "2026-09-10T07:00:00"
