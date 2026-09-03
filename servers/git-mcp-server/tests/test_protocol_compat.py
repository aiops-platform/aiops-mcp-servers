"""端到端 MCP 协议测试（Streamable HTTP over TestClient）：初始化→tools/list→tools/call。"""
from __future__ import annotations

from git_mcp_server.server import build_app
from starlette.testclient import TestClient

_ACCEPT = "application/json, text/event-stream"
_HEADERS = {"Accept": _ACCEPT, "Authorization": "Bearer sekret"}


def _initialize(c: TestClient, headers=None) -> str:
    r = c.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.0.1"},
            },
        },
        headers=headers or _HEADERS,
    )
    assert r.status_code == 200, r.text
    return r.headers.get("mcp-session-id")


def test_initialize_returns_protocol_version(env, monkeypatch, git_repo):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with TestClient(build_app()) as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.0.1"},
                },
            },
            headers=_HEADERS,
        )
        assert r.status_code == 200
        result = r.json()["result"]
        assert result["protocolVersion"]
        assert "tools" in result["capabilities"]


def test_tools_list_has_all_six(env, monkeypatch, git_repo):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with TestClient(build_app()) as c:
        sid = _initialize(c)
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**_HEADERS, "MCP-Session-Id": sid},
        )
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {
            "get_repo_status",
            "get_commit_log",
            "get_commit_detail",
            "list_branches",
            "search_code",
            "blame_file",
        }


def test_tools_call_get_repo_status(env, monkeypatch, git_repo):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with TestClient(build_app()) as c:
        sid = _initialize(c)
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_repo_status",
                    "arguments": {"repo_path": str(git_repo)},
                },
            },
            headers={**_HEADERS, "MCP-Session-Id": sid},
        )
        result = r.json()["result"]
        assert result["isError"] is False
        payload = result["content"][0]["text"]
        assert '"clean": true' in payload
        assert '"branch"' in payload


def test_auth_required_on_mcp(env, monkeypatch, git_repo):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with TestClient(build_app()) as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            },
            headers={"Accept": _ACCEPT},  # 无 Authorization
        )
        assert r.status_code in (401, 403)


def test_unknown_tool_returns_error(env, monkeypatch, git_repo):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    with TestClient(build_app()) as c:
        sid = _initialize(c)
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            },
            headers={**_HEADERS, "MCP-Session-Id": sid},
        )
        assert "error" in r.json() or r.json()["result"]["isError"] is True
