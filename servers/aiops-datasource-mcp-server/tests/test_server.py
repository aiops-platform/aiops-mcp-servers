"""ASGI 端到端：/health + MCP initialize → tools/list → tools/call（真实协议路径）。"""
from __future__ import annotations

from starlette.testclient import TestClient

_ACCEPT = {"Accept": "application/json, text/event-stream"}


def _client(env) -> TestClient:
    from aiops_datasource_mcp_server.server import build_app

    return TestClient(build_app())


def _init(c: TestClient) -> str:
    r = c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=_ACCEPT,
    )
    assert r.status_code == 200, r.text
    return r.headers["mcp-session-id"]


def _list_tools(c: TestClient, sid: str) -> list[dict]:
    r = c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers={**_ACCEPT, "MCP-Session-Id": sid},
    )
    return r.json()["result"]["tools"]


def _call(c: TestClient, sid: str, name: str, args: dict) -> dict:
    r = c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers={**_ACCEPT, "MCP-Session-Id": sid},
    )
    return r.json()["result"]


def test_health(env) -> None:
    with _client(env) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["service"] == "aiops-datasource-mcp-server"


def test_tools_list_exposes_all_read_only(env) -> None:
    with _client(env) as c:
        tools = _list_tools(c, _init(c))
    names = {t["name"] for t in tools}
    assert names == {
        "query_logs", "get_trace", "query_metrics", "check_infra", "describe_pod",
        "get_service_topology", "locate_repo",
    }
    # 只读注解：agent 侧（AgentScope）据此自动 ALLOW
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)


def test_time_window_is_required_in_schema(env) -> None:
    """时间区间必须是 schema 里的必填项——这是 v5.5 的核心约定。"""
    with _client(env) as c:
        tools = {t["name"]: t for t in _list_tools(c, _init(c))}

    for name in ("query_logs", "get_trace", "query_metrics"):
        required = tools[name]["inputSchema"].get("required") or []
        assert "start_time" in required, f"{name} 必须要求 start_time"
        assert "end_time" in required, f"{name} 必须要求 end_time"

    # 状态查询工具无时间参数（设计如此）
    for name in ("check_infra", "describe_pod"):
        props = tools[name]["inputSchema"].get("properties") or {}
        assert "start_time" not in props


def test_call_with_invalid_window_returns_error(env) -> None:
    """非法时间区间 → 工具调用失败（isError），且不产生上游请求。"""
    with _client(env) as c:
        sid = _init(c)
        res = _call(c, sid, "query_logs", {
            "start_time": "not-a-time", "end_time": "2026-09-10T07:00:00",
        })
    assert res["isError"] is True
    assert "ISO8601" in res["content"][0]["text"]


def test_call_with_unknown_metric_returns_error_with_options(env) -> None:
    with _client(env) as c:
        sid = _init(c)
        res = _call(c, sid, "query_metrics", {
            "service": "order-service", "metric": "bogus",
            "start_time": "2026-09-10T06:00:00", "end_time": "2026-09-10T07:00:00",
        })
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "未知 metric" in text
    assert "cpu_percent" in text  # 给出可用清单供纠正


def test_call_missing_required_time_returns_error(env) -> None:
    """缺必填时间参数 → FastMCP 层校验失败（不会调用到后端）。"""
    with _client(env) as c:
        sid = _init(c)
        res = _call(c, sid, "query_logs", {})
    assert res["isError"] is True
