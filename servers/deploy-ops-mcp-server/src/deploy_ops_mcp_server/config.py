"""统一配置（pydantic-settings），全部可通过 .env 或环境变量覆盖。

命名约定（对齐 aiops-datasource-mcp-server / applog-mcp-server）：共享/基础设施变量不加
前缀（ENVIRONMENT / BIND_HOST / BIND_PORT / AUTH_TOKEN / LOG_LEVEL），
本 server 领域变量统一 ``DEPLOY_OPS_`` 前缀。
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- 运行环境 ---
    environment: str = "development"  # development | production（production 强制认证）

    # --- 服务 ---
    bind_host: str = "127.0.0.1"
    # 8400：git=8000/8100、applog=8200/8101、datasource=8300 之后的下一个
    # （四处保持一致：config.py / .env.example / README / agentflow 的 .env）
    bind_port: int = 8400

    # --- MCP 认证（可选） ---
    # 静态 Bearer Token；为空则不启用认证；production 下为空则拒绝启动
    auth_token: str = ""

    # --- 集群 CLI ---
    # 三者都走 PATH 查找；容器形态下要显式给绝对路径
    deploy_ops_kubectl_bin: str = "kubectl"
    deploy_ops_minikube_bin: str = "minikube"
    # docker CLI —— **只用于启动自检**（本 server 不 build 镜像）
    deploy_ops_docker_bin: str = "docker"
    # kubeconfig 路径；空 = 用 kubectl 自身默认（~/.kube/config 或 in-cluster）
    deploy_ops_kubeconfig: str = ""

    # --- 部署目标表（**本 server 自己持有**，见 DEPLOY_NODE_PLAN_zh-CN.md §4）---
    # JSON：`{"<service>": {"namespace": ..., "deployment": ..., "container": ...}}`
    #
    # 为什么不从 agentflow 传进来：那三个是**集群侧的事实**（与 action_executor._check_ns
    # 同一判据：namespace 该由持有集群凭证的那一方裁决），而且让平台/模型传就多一个
    # "传错"的面 —— 滚成功但滚错地方比失败更难查。签名因此只收 `service`。
    #
    # **缺条 ⇒ 工具 fail-closed 报错**，不猜一个默认命名空间。
    deploy_ops_targets: str = ""

    # --- 超时（三个，别混）---
    # ① `kubectl rollout status --timeout=<N>s` —— **kubectl 自己的等待预算**，
    #    也是"多久判定这次滚动没成功"的判据。
    deploy_ops_rollout_timeout_sec: int = Field(default=300, gt=0)
    # ② 子进程的墙钟上限（set image / rollout status 用）。
    #    ⚠️ **必须大于 ①**，否则会在 kubectl 自己超时之前把它杀掉，而报错变成
    #    "命令超时"——**分不清是滚动慢还是被我们砍了**。校验见下方 model_validator。
    deploy_ops_subprocess_timeout_sec: float = Field(default=360.0, gt=0)
    # ③ `minikube image load` 的墙钟上限。单独一个是因为它的量级完全不同：
    #    要把镜像（产物 35MB + 基础层 415MB）灌进节点 containerd，冷启动分钟级。
    #    实测值待补（见 DEPLOY_NODE_PLAN_zh-CN.md §6 风险 4）。
    deploy_ops_image_load_timeout_sec: float = Field(default=900.0, gt=0)
    # 启动自检的超时（要短：起不来就赶紧报，别让人等半分钟）
    deploy_ops_probe_timeout_sec: float = Field(default=15.0, gt=0)
    # --- `probe_service` 的两个超时 ---
    # `kubectl port-forward` 从起到可连的上限。实测秒级（日志出现 `Forwarding from` 后 4s 内可打），
    # 给 20s 是给冷启动留余量；超了 ⇒ `forward_failed`，**不留下进程**。
    deploy_ops_forward_ready_timeout_sec: float = Field(default=20.0, gt=0)
    # 单条 HTTP 探针的请求超时。
    deploy_ops_probe_http_timeout_sec: float = Field(default=10.0, gt=0)

    # --- 响应 ---
    # 单次工具返回字节上限（超过截断并标注）
    deploy_ops_max_response_bytes: int = Field(default=256 * 1024, gt=0)  # 256KB

    # --- 日志 ---
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _check_timeouts_and_targets(self) -> Settings:
        """构造时就拦住两类配置错误 —— 都是"跑到一半才炸、且症状指到别处"那类。"""
        # ① 小的那个会**静默覆盖**大的：子进程墙钟若小于 kubectl 的等待预算，
        #    滚动慢的时候我们会先把它杀掉，而报错说"命令超时"，看不出是配置问题。
        if self.deploy_ops_subprocess_timeout_sec <= self.deploy_ops_rollout_timeout_sec:
            raise ValueError(
                f"DEPLOY_OPS_SUBPROCESS_TIMEOUT_SEC({self.deploy_ops_subprocess_timeout_sec}) "
                f"必须大于 DEPLOY_OPS_ROLLOUT_TIMEOUT_SEC({self.deploy_ops_rollout_timeout_sec})"
                "——否则会在 kubectl 自己超时之前把它杀掉，而报错分不清"
                "「滚动慢」与「被我们砍了」"
            )
        # ② 目标表：非法 JSON 或条目缺字段，都在启动时拦住（不是第一次调用时）
        for service, spec in self.targets.items():
            missing = [k for k in ("namespace", "deployment", "container") if not spec.get(k)]
            if missing:
                raise ValueError(
                    f"DEPLOY_OPS_TARGETS['{service}'] 缺字段 {missing} "
                    "—— 三个都要给（部署目标由本 server 持有，没有兜底默认值）"
                )
        return self

    @property
    def targets(self) -> dict[str, dict]:
        """部署目标表（已解析）。空 = 未配置 ⇒ 任何 rollout 调用都 fail-closed。"""
        raw = (self.deploy_ops_targets or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DEPLOY_OPS_TARGETS 不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("DEPLOY_OPS_TARGETS 必须是 JSON 对象（service → 目标）")
        return parsed

    def target_for(self, service: str) -> dict:
        """按服务名取部署目标。**缺条即抛**（不猜一个默认命名空间）。"""
        from .errors import AppError, ErrorCode

        spec = self.targets.get(service)
        if not spec:
            raise AppError(
                ErrorCode.CONFIG_ERROR,
                f"service={service!r} 不在 DEPLOY_OPS_TARGETS 里"
                f"（已配置：{sorted(self.targets)}）。部署目标由本 server 持有，"
                "没有兜底默认值 —— 宁可报错，也不滚到别的命名空间去。",
                {"tool": "rollout_deployment", "service": service},
            )
        return spec

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
