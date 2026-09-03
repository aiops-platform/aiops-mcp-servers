"""ASGI 应用组装测试：/health、/metrics、认证强制、路由结构。"""
from __future__ import annotations

from git_mcp_server.server import build_app
from starlette.testclient import TestClient


def test_health_ok(env):
    with TestClient(build_app()) as c:
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "git-mcp-server"


def test_metrics_expose_prometheus(env):
    with TestClient(build_app()) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "git_mcp_requests_total" in r.text


def test_health_is_public_even_with_host_blocked(env, monkeypatch):
    """/health 不经过 Host 校验，监控探活不受影响。"""
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    with TestClient(build_app()) as c:
        r = c.get("/health", headers={"Host": "evil.example.com"})
        assert r.status_code == 200


def test_mcp_requires_auth_in_production(env, monkeypatch):
    """配置了 AUTH_TOKEN 时，无凭据请求 /mcp 被拒。"""
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    monkeypatch.setenv("GIT_ALLOWED_ROOTS", "/tmp")
    with TestClient(build_app()) as c:
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code in (401, 403)


def test_mcp_accepts_valid_auth(env, monkeypatch, git_repo):
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
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer sekret",
            },
        )
        assert r.status_code == 200


def test_tools_enabled_filter(env, monkeypatch, git_repo):
    """TOOLS_ENABLED 只注册指定工具。"""
    from git_mcp_server.server import create_mcp_server

    monkeypatch.setenv("GIT_ALLOWED_ROOTS", str(git_repo))
    monkeypatch.setenv("TOOLS_ENABLED", "get_repo_status,search_code")
    mcp = create_mcp_server()
    with TestClient(build_app(mcp)) as c:
        h = {"Accept": "application/json, text/event-stream"}
        init = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers=h,
        )
        sid = init.headers.get("mcp-session-id")
        tools = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**h, "MCP-Session-Id": sid},
        ).json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {"get_repo_status", "search_code"}
