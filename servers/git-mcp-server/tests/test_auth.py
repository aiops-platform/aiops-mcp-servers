"""StaticTokenVerifier 认证测试。"""
from __future__ import annotations

import pytest
from git_mcp_server.auth.token_verifier import StaticTokenVerifier


async def _verify(token: str):
    return await StaticTokenVerifier().verify_token(token)


@pytest.mark.asyncio
async def test_valid_token(env, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    result = await _verify("sekret")
    assert result is not None
    assert result.client_id == "git-mcp-server"
    assert "git:read" in result.scopes


@pytest.mark.asyncio
async def test_invalid_token(env, monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "sekret")
    assert await _verify("wrong") is None


@pytest.mark.asyncio
async def test_fail_closed_when_no_token_configured(env):
    """未配置 AUTH_TOKEN → 拒绝所有（fail-closed）。"""
    assert await _verify("anything") is None


@pytest.mark.asyncio
async def test_token_verifier_is_async_callable(env):
    """符合 mcp TokenVerifier 协议：可被 await。"""
    verifier = StaticTokenVerifier()
    assert hasattr(verifier, "verify_token")
    assert verifier.verify_token is not None
