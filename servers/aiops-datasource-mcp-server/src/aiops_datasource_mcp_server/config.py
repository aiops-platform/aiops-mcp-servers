"""统一配置（pydantic-settings），全部可通过 .env 或环境变量覆盖。

命名约定（对齐 applog-mcp-server）：共享/基础设施变量不加前缀（ENVIRONMENT /
BIND_HOST / BIND_PORT / AUTH_TOKEN / LOG_LEVEL），本 server 领域变量统一
``DATASOURCE_`` 前缀。
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- 运行环境 ---
    environment: str = "development"  # development | production（production 强制认证）

    # --- 服务 ---
    bind_host: str = "127.0.0.1"
    # 8300：git=8000/8100、applog=8200/8101 之后的下一个（四处保持一致：
    # config.py / .env.example / README / Dockerfile）
    bind_port: int = 8300

    # --- MCP 认证（可选） ---
    # 静态 Bearer Token；为空则不启用认证；production 下为空则拒绝启动
    auth_token: str = ""

    # --- Elasticsearch ---
    datasource_es_url: str = "http://localhost:19200"
    # 日志索引名；测试床为 app-logs（字段在 app.* 下，如 app.service / app.level）
    datasource_es_index: str = "app-logs"

    # --- Prometheus ---
    datasource_prom_url: str = "http://localhost:19090"

    # --- Kubernetes ---
    # check_infra / describe_pod 的默认 namespace（未显式传参时使用）
    datasource_k8s_namespace: str = "order"
    # kubectl 二进制路径（空 = 走 PATH 查找）
    datasource_kubectl_bin: str = "kubectl"
    # kubeconfig 路径；空 = 用 kubectl 自身默认（~/.kube/config 或 in-cluster）
    datasource_kubeconfig: str = ""

    # --- CMDB / 仓库定位 ---
    # 仓库远端根（`locate_repo` 在未配本地 root 时返回
    # `https://github.com/{org}/{repo}`）
    datasource_repo_org: str = "acme-aiops"
    # 本地仓库根目录（testbed 联调）：**非空**时 `locate_repo` 返回
    # `file://{root}/{repo}` 而非远端 URL；空 = 走远端。
    # 例：/path/to/agentflow-testbed/services
    # 注意：这里没有默认的个人绝对路径——本机路径由各环境自己的 .env 提供。
    datasource_repo_root: str = ""
    # `get_service_topology` 默认跳数（调用方仍可显式覆盖）
    datasource_topology_default_hops: int = Field(default=2, ge=0, le=6)

    # --- 请求与配额 ---
    # 单次上游请求超时（秒）
    datasource_request_timeout_sec: float = Field(default=30.0, gt=0)
    # 单次工具返回字节上限（超过截断并标注）
    datasource_max_response_bytes: int = Field(default=1024 * 1024, gt=0)  # 1MB
    # 单次查询允许的最大时间跨度（小时）——防止全量扫描拖垮数据源
    datasource_max_range_hours: float = Field(default=24.0, gt=0)
    # query_metrics 默认采样步长（秒）
    datasource_default_step_sec: int = Field(default=30, gt=0)

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
