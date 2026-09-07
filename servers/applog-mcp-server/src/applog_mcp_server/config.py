"""统一配置（pydantic-settings），全部可通过 .env 或环境变量覆盖。

工具定义（HTTP 接口 → MCP tool）不在 env，在 APPLOG_TOOLS_FILE（默认 config/tools.yaml）声明。
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- 运行环境 ---
    environment: str = "development"  # development | production（production 强制认证）

    # --- 服务 ---
    bind_host: str = "127.0.0.1"
    bind_port: int = 8200

    # --- MCP 认证（可选） ---
    # 静态 Bearer Token；为空则不启用认证；production 下为空则拒绝启动
    auth_token: str = ""

    # --- 上游日志接口 ---
    # tools.yaml 里 http.base_url 缺省时回落到这里
    applog_default_base_url: str = "http://localhost:8080"
    # 工具定义文件路径（相对运行目录或绝对路径）
    applog_tools_file: str = "config/tools.yaml"
    # 单次上游请求超时（秒）
    applog_request_timeout_sec: float = Field(default=30.0, gt=0)
    # 单次工具返回字节上限（超过截断并标注）
    applog_max_response_bytes: int = Field(default=1024 * 1024, gt=0)  # 1MB

    # --- 日志 ---
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def validate_for_environment(self) -> None:
        """启动前校验：production 下必须配置认证，防止服务裸奔。"""
        if self.environment != "production":
            return
        if not self.auth_token:
            raise ValueError(
                "ENVIRONMENT=production 要求配置 AUTH_TOKEN，否则拒绝启动（防止服务裸奔）"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
