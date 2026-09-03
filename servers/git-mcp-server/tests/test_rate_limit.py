"""RateLimitMiddleware 测试：Token Bucket 限流与 429。"""
from __future__ import annotations

from git_mcp_server.middleware.rate_limit import RateLimitMiddleware, TokenBucket
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def test_token_bucket_allows_up_to_capacity():
    bucket = TokenBucket(capacity=3, refill_per_second=0.1)
    assert [bucket.take() for _ in range(3)] == [True, True, True]
    assert bucket.take() is False


def test_token_bucket_refills_over_time():
    import time

    bucket = TokenBucket(capacity=1, refill_per_second=1.0)
    assert bucket.take() is True
    assert bucket.take() is False
    time.sleep(1.1)
    assert bucket.take() is True


def _rate_limited_app():
    async def ok(_request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/", ok)])
    return RateLimitMiddleware(inner)


def test_disabled_by_default(env, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    with TestClient(_rate_limited_app()) as c:
        for _ in range(10):
            assert c.get("/").status_code == 200


def test_rate_limit_exceeded(env, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    with TestClient(_rate_limited_app()) as c:
        assert [c.get("/").status_code for _ in range(3)] == [200, 200, 200]
        r = c.get("/")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
