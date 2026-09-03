"""统一配置（pydantic-settings），全部可通过 .env 或环境变量覆盖。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- 运行环境 ---
    environment: str = "development"  # development | production（production 强制认证）

    # --- 服务 ---
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # --- 认证 ---
    # 静态 Bearer Token；production 下为空则拒绝启动
    auth_token: str = ""
    # OAuth 2.1 模式时的 issuer（本期可选，静态 token 可不填）
    auth_issuer_url: str = ""

    # --- Git 沙箱与资源限制 ---
    git_allowed_roots: str = ""  # 逗号分隔；为空则拒绝所有仓库（fail-closed）
    git_command_timeout_sec: float = 30.0
    git_max_output_bytes: int = 1024 * 1024  # 1MB

    # --- Origin / Host 校验（防 DNS rebinding，MCP 规范 MUST） ---
    allowed_origins: str = ""  # 逗号分隔；为空则仅放行无 Origin 的请求
    allowed_hosts: str = "localhost,127.0.0.1,::1"  # 逗号分隔；默认回环白名单

    # --- 限流 ---
    rate_limit_enabled: bool = False
    rate_limit_max_requests: int = 100
    rate_limit_window_seconds: int = 60

    # --- 工具开关（可选加分项） ---
    tools_enabled: str = ""  # 逗号分隔工具名；为空则全部启用

    # --- 可观测性 ---
    metrics_enabled: bool = True
    log_level: str = "INFO"

    # --- OpenTelemetry（可选，需安装 [otel] extra） ---
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- 便捷属性 ---
    @property
    def allowed_roots_list(self) -> list[str]:
        return self._csv(self.git_allowed_roots)

    @property
    def allowed_origins_list(self) -> list[str]:
        return self._csv(self.allowed_origins)

    @property
    def allowed_hosts_list(self) -> list[str]:
        return self._csv(self.allowed_hosts)

    @property
    def tools_enabled_list(self) -> list[str]:
        return self._csv(self.tools_enabled)

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def validate_for_environment(self) -> None:
        """启动前校验：production 下必须配置认证与仓库白名单，防止服务裸奔。"""
        if self.environment != "production":
            return
        if not self.auth_token:
            raise ValueError(
                "ENVIRONMENT=production 要求配置 AUTH_TOKEN，否则拒绝启动（防止服务裸奔）"
            )
        if not self.allowed_roots_list:
            raise ValueError(
                "ENVIRONMENT=production 要求配置 GIT_ALLOWED_ROOTS，否则拒绝启动"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
