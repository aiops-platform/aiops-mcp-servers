"""静态 Bearer Token 校验器（FastMCP v1 原生认证）。

实现官方 mcp SDK 的 `TokenVerifier` 协议，注入 `FastMCP(token_verifier=...)`，
认证由 SDK 在 /mcp 端点真正生效（无需自建认证中间件）。

生产环境建议切换为 OAuth 2.1 / RFC 7662 introspection（见 docs/git-mcp-server-design.md）。
"""
from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken

from git_mcp_server.config import get_settings


class StaticTokenVerifier:
    """校验请求中的 Bearer Token 是否匹配配置的静态 token（fail-closed）。"""

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()

        # 未配置 token → 拒绝所有（fail-closed），与服务启动校验保持一致
        if not settings.auth_token:
            return None

        if hmac.compare_digest(token, settings.auth_token):
            return AccessToken(
                token=token,
                client_id="git-mcp-server",
                scopes=["git:read"],
            )
        return None
