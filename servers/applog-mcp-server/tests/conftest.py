"""共享 fixtures：清配置缓存 + 指向临时 tools.yaml 的环境 + 默认工具文件。"""
from __future__ import annotations

import textwrap

import pytest
from applog_mcp_server.config import get_settings

DEFAULT_TOOLS_YAML = textwrap.dedent(
    """\
    tools:
      - name: query_app_logs
        description: 按时间范围 + 日志级别 + 服务名查询应用日志
        http:
          method: GET
          path: /api/sip-aiops/app-log
          base_url: http://upstream.test
        inputs:
          - name: startTime
            type: string
            in: query
            required: true
            description: 开始时间
          - name: endTime
            type: string
            in: query
            required: true
            description: 结束时间
          - name: logLevel
            type: string
            in: query
            required: false
            description: 日志级别
          - name: serviceName
            type: string
            in: query
            required: false
            description: 服务名
        response:
          mode: passthrough
    """
)


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch, tmp_path, clear_settings_cache):
    """把 Settings 指向临时 tools.yaml，并隔离常用 env。"""
    tools = tmp_path / "tools.yaml"
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("APPLOG_DEFAULT_BASE_URL", "http://default-upstream.test")
    monkeypatch.setenv("APPLOG_TOOLS_FILE", str(tools))
    monkeypatch.setenv("APPLOG_REQUEST_TIMEOUT_SEC", "5.0")
    monkeypatch.setenv("APPLOG_MAX_RESPONSE_BYTES", "1024")
    return tools


@pytest.fixture
def tools_file(env):
    """在 env 指向的临时文件里写入默认单工具定义。"""
    env.write_text(DEFAULT_TOOLS_YAML, encoding="utf-8")
    return env
