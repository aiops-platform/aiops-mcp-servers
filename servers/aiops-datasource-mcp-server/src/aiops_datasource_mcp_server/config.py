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
    # CMDB 实体图谱文件（**CMDB 的唯一载体**）。**空 = 用包内自带的
    # `data/cmdb-entities.json`**——包内路径与 cwd 无关，随 wheel 分发。
    # 生产/多租户请指向挂载卷上可授权编辑的文件。
    # 该文件缺失是**配置错误**：CMDB 工具会 fail-closed 报错（不给空图，
    # 免得把"文件没挂上"洗成"这服务没有依赖"）。
    datasource_cmdb_path: str = ""
    # 可选的事件覆盖文件（Incident / Change 节点，与主文件同 schema）。
    # **空 = 无事件数据——这是合法状态，不是错误**。刻意与 datasource_cmdb_path
    # 的 fail-closed 语义相反：主文件缺失是配置问题，事件文件缺失只是还没数据。
    datasource_incidents_path: str = ""
    # CMDB 管理端点（`/admin/cmdb/**`，可写）开关。**三态**：
    #   未设置（默认）→ development 下可用、production 下**关闭**
    #   true          → 显式打开（production 下要配合 AUTH_TOKEN；启动校验会强制要求）
    #   false         → 显式关闭
    # 默认不能在 production 打开，是因为这是一个**能改诊断数据源**的写端点：
    # 它不该因为"部署时忘了关"而暴露。要开就显式开。见 admin_enabled。
    datasource_admin_enabled: bool | None = None

    # --- APM 工单回传（`returnApmTicketStatus`，本 server 唯一的对外写面）---
    # 回传的「谓词 + URI」在这里配 —— **不由 agent 传**。回调地址是**部署属性**，
    # 不是这次 run 的属性：让 agent 传等于让 LLM 决定往哪儿 POST，且同一原系统
    # 每张工单都要重传一遍，传错/漏传就静默投不出去。agent 只给三个业务字段。
    #
    # **空 = 未配置 ⇒ 工具 fail-closed 报错**，绝不假装投递成功。
    # 例：DATASOURCE_APM_TICKET_URL=https://apm.internal/api/v1/incidents/callback
    datasource_apm_ticket_url: str = ""
    # 谓词。原系统的回调端点若不是 POST，在这里改（GET 无 body，一般不用）。
    datasource_apm_ticket_method: str = "POST"

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

    @property
    def admin_enabled(self) -> bool:
        """CMDB 写端点是否可用。未显式配置时**按环境默认**：development 开、production 关。"""
        if self.datasource_admin_enabled is not None:
            return self.datasource_admin_enabled
        return self.environment != "production"

    def validate_for_environment(self) -> None:
        """启动前校验：production 下必须配置认证，防止服务裸奔。"""
        if self.environment != "production":
            return
        if not self.auth_token:
            raise ValueError(
                "ENVIRONMENT=production 要求配置 AUTH_TOKEN，否则拒绝启动（防止服务裸奔）"
            )
        # 打开了 CMDB 写端点就一定要有认证——上面那条已保证 auth_token 非空，
        # 这里只是把意图写明：这两件事必须同时成立，不能靠"碰巧"。
        if self.datasource_admin_enabled and not self.auth_token:
            raise ValueError(
                "DATASOURCE_ADMIN_ENABLED=true 要求配置 AUTH_TOKEN"
                "（CMDB 写端点绝不能无认证暴露）"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
