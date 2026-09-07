"""ASGI 端到端测试：/health、认证开关、tools/list 注册与只读标注。"""
from __future__ import annotations

import pytest
from applog_mcp_server.server import build_app, create_mcp_server
from starlette.testclient import TestClient


def _init(c, *, token: str | None = None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers=headers,
    )


def _list_tools(c, sid):
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Session-Id": sid,
        },
    )
    return r.json()["result"]["tools"]


def _call(c, sid, name, arguments):
    return c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Session-Id": sid,
        },
    )


def test_tools_call_missing_required_returns_error(tools_file):
    """缺必填入参走 FastMCP 真实校验，返回错误而非落上游请求。"""
    with TestClient(build_app()) as c:
        sid = _init(c).headers.get("mcp-session-id")
        r = _call(c, sid, "query_app_logs", {"startTime": "t0"})
    assert r.status_code == 200
    body = r.json()
    if "error" in body:
        assert body["error"]["code"] != 0  # JSON-RPC error 分支
    else:
        assert body["result"]["isError"] is True


def test_health_ok(tools_file):
    with TestClient(build_app()) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "applog-mcp-server"


def test_tools_list_registers_spec_and_readonly_hint(tools_file):
    with TestClient(build_app()) as c:
        init = _init(c)
        assert init.status_code == 200
        sid = init.headers.get("mcp-session-id")
        tools = _list_tools(c, sid)
    names = {t["name"] for t in tools}
    assert names == {"query_app_logs"}

    tool = next(t for t in tools if t["name"] == "query_app_logs")
    # 入参 schema 由 spec.inputs 生成：必填项进入 required
    schema = tool["inputSchema"]
    assert schema["required"] == ["startTime", "endTime"]
    assert set(schema["properties"]) >= {"startTime", "endTime", "logLevel", "serviceName"}
    # 只读标注（agent 侧据此 ALLOW）
    assert tool["annotations"]["readOnlyHint"] is True


def test_auth_off_by_default_no_token_needed(tools_file):
    with TestClient(build_app()) as c:
        assert _init(c).status_code == 200


def test_auth_on_rejects_anonymous(tools_file, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    with TestClient(build_app()) as c:
        assert _init(c).status_code in (401, 403)


def test_auth_on_accepts_valid_token(tools_file, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    with TestClient(build_app()) as c:
        assert _init(c, token="sekret").status_code == 200


def test_production_requires_auth_token(tools_file, monkeypatch):
    """production + 空 token：直接走 create_mcp_server（绕过 main）也必须拒绝启动。"""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "")
    with pytest.raises(ValueError, match="production"):
        create_mcp_server()


def test_production_with_token_starts(tools_file, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    with TestClient(build_app()) as c:
        assert _init(c, token="sekret").status_code == 200


def test_duplicate_spec_name_rejected_at_startup(env):
    """tools.yaml 里名字重复 → 启动期 fail-closed（build_app 默认建 server 即失败）。"""
    env.write_text(
        "tools:\n"
        "  - {name: q, http: {method: GET, path: /a}}\n"
        "  - {name: q, http: {method: GET, path: /b}}\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="名字重复"):
        create_mcp_server()
