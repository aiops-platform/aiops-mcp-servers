"""静态 Bearer Token 校验器（FastMCP v1 原生认证）。

实现官方 mcp SDK 的 `TokenVerifier` 协议，注入 `FastMCP(token_verifier=...)`，
认证由 SDK 在 /mcp 端点真正生效（无需自建认证中间件）。

⚠️ 与本 server 的写面相关：`rollout_deployment` 会改线上。development 下不配
AUTH_TOKEN 是允许的（本地联调），但 **production 下 validate_for_environment
会拒绝启动** —— 一台能滚线上的 server 不能裸奔。
"""
from __future__ import annotations

import hmac
import logging

from mcp.server.auth.provider import AccessToken

from deploy_ops_mcp_server.config import get_settings

logger = logging.getLogger("deploy_ops_mcp_server.auth")


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
                client_id="deploy-ops-mcp-server",
                scopes=["deploy:write"],
            )
        # 不匹配：记审计日志（勿记录 token 明文）
        logger.warning("rejected bearer token authentication attempt (token mismatch)")
        return None
