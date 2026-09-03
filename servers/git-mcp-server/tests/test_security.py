"""SecurityMiddleware 测试：Host/Origin 校验、trace_id、公开端点放行。"""
from __future__ import annotations

from git_mcp_server.middleware.security import SecurityMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient


def _app() -> Starlette:
    async def ok(_request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    return Starlette(routes=[Route("/health", ok, methods=["GET"]), Mount("/", app=inner)])


def _client(env, **kwargs) -> TestClient:
    app = _app()
    wrapped = SecurityMiddleware(app)
    return TestClient(wrapped)


def test_allowed_host_passes(env, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    with _client(env) as c:
        r = c.post("/mcp", json={}, headers={"Host": "testserver"})
        assert r.status_code == 200


def test_disallowed_host_blocked(env, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    with _client(env) as c:
        r = c.post("/mcp", json={}, headers={"Host": "evil.example.com"})
        assert r.status_code == 403


def test_origin_allowed(env, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com")
    with _client(env) as c:
        r = c.post(
            "/mcp", json={}, headers={"Host": "testserver", "Origin": "https://app.example.com"}
        )
        assert r.status_code == 200


def test_disallowed_origin_blocked(env, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com")
    with _client(env) as c:
        r = c.post("/mcp", json={}, headers={"Host": "testserver", "Origin": "https://evil.example.com"})
        assert r.status_code == 403


def test_origin_fail_closed_when_empty(env, monkeypatch):
    """ALLOWED_ORIGINS 为空 → 仅放行无 Origin 的请求。"""
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    with _client(env) as c:
        r = c.post("/mcp", json={}, headers={"Host": "testserver", "Origin": "https://app.example.com"})
        assert r.status_code == 403
        # 无 Origin → 放行
        r2 = c.post("/mcp", json={}, headers={"Host": "testserver"})
        assert r2.status_code == 200


def test_public_health_never_blocked(env, monkeypatch):
    """/health 不受 Host/Origin 校验影响（公开端点）。"""
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")
    with _client(env) as c:
        r = c.get("/health", headers={"Host": "evil.example.com"})
        assert r.status_code == 200


def test_trace_id_in_state(env, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    seen: dict = {}

    async def capture(request):
        seen["trace_id"] = request.scope["state"].get("trace_id")
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/mcp", capture, methods=["GET", "POST"])])
    wrapped = SecurityMiddleware(Starlette(routes=[Mount("/", app=inner)]))
    with TestClient(wrapped) as c:
        c.post("/mcp", json={}, headers={"Host": "testserver"})
    assert seen.get("trace_id") is not None
