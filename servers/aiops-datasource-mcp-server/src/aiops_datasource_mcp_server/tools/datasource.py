"""5 个领域型只读工具（ES 日志 / Prometheus 指标 / K8s 状态）。

设计要点（design-v5.5 §4/§5）：

- **查询必须指定时间区间与目标**：`query_logs` / `get_trace` / `query_metrics`
  的 `start_time` / `end_time` 是必填参数，区间经 fail-closed 校验（格式、先后、
  跨度上限）后才下发；响应回显解析后的窗口，让调用方确知实际查了什么。
- **`check_infra` / `describe_pod` 无时间参数**——查的是集群当前状态，
  「查询目标」即 namespace / pod。
- 签名**字面书写**（不用 exec 生成），`Annotated[..., Field(...)]` 让 FastMCP
  生成 JSON Schema 并做真实校验。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

from aiops_datasource_mcp_server.backends import es, k8s
from aiops_datasource_mcp_server.backends import prometheus as prom
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# ----------------------------------------------------------------------
# 共用参数类型（中英双语描述，与 git-mcp-server 风格一致）
# ----------------------------------------------------------------------
StartTime = Annotated[
    str,
    Field(description=(
        "开始时间（ISO8601，含边界），如 2026-09-10T06:00:00 或 "
        "2026-09-10T06:00:00+00:00。必填——本 server 不提供无窗口的全量查询。"
    )),
]
EndTime = Annotated[
    str,
    Field(description=(
        "结束时间（ISO8601，含边界），须晚于 start_time。"
        f"区间跨度上限 {get_settings().datasource_max_range_hours:g} 小时。"
    )),
]
ServiceType = Annotated[
    str,
    Field(description="服务名（如 order-service / warranty-service），作为查询目标"),
]
NamespaceType = Annotated[
    str,
    Field(description="K8s namespace，如 order"),
]

# 注意：本模块用了 `from __future__ import annotations`，函数签名的注解会被存成**字符串**
# 交给 FastMCP 的 get_type_hints 惰性求值。因此注解里引用的名字必须能在**模块全局**解析——
# 不能用函数内闭包变量（那样 InvalidSignature）。描述文本一律提升到模块级常量。
_METRIC_DESC = (
    "指标名（**领域语义，不是 PromQL 表达式**）。可用："
    + ", ".join(prom.available_metrics())
    + "。传其它值会直接报错并列出可用项——本 server 不做静默兜底。"
)



def _parse_window(start_time: str, end_time: str) -> tuple[datetime, datetime]:
    """解析并校验时间区间（fail-closed）。

    任一不合法即 ``AppError(INVALID_REQUEST)``——**不做宽容解析、不静默取默认值**：
    查询窗口错了，返回的数据就没有意义，早失败远好过给出一份看似正常的答案。
    """
    def _parse(raw: str, field: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"{field} 不是合法 ISO8601 时间：{raw!r}。"
                "示例：2026-09-10T06:00:00 或 2026-09-10T06:00:00Z",
            ) from exc
        # naive 时间视为 UTC，与 ES / Prometheus 的时区语义对齐
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    start = _parse(start_time, "start_time")
    end = _parse(end_time, "end_time")
    if start >= end:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"start_time 必须早于 end_time（当前 {start.isoformat()} ≥ {end.isoformat()}）",
        )

    max_hours = get_settings().datasource_max_range_hours
    span_hours = (end - start).total_seconds() / 3600
    if span_hours > max_hours:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"时间区间 {span_hours:.1f} 小时超过上限 {max_hours:g} 小时——"
            "请收窄窗口（诊断应聚焦故障发生的那一段时间）",
        )
    return start, end


# ======================================================================
# 工具工厂
# ======================================================================
def _build_query_logs():
    async def query_logs(
        start_time: StartTime,
        end_time: EndTime,
        service: Annotated[
            str | None, Field(description="服务名过滤（如 warranty-service）；不传则全服务")
        ] = None,
        level: Annotated[
            str | None, Field(description="日志级别过滤，如 ERROR / WARN / INFO")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=200, description="最多返回条数（1-200，默认 50）")
        ] = 50,
    ) -> dict:
        """按**时间窗口**检索应用日志（Elasticsearch）。

        时间区间必填；可按服务与级别过滤。返回日志明细、服务分布与该窗口。
        """
        start, end = _parse_window(start_time, end_time)
        return await es.query_logs(start, end, service=service, level=level, limit=limit)

    return query_logs


def _build_get_trace():
    async def get_trace(
        trace_id: Annotated[
            str, Field(description="链路 ID（traceId），按它重建整条调用链")
        ],
        start_time: StartTime,
        end_time: EndTime,
        limit: Annotated[
            int, Field(ge=1, le=200, description="链路内最多取多少条日志（默认 100）")
        ] = 100,
    ) -> dict:
        """按 trace_id + **时间窗口**重建调用链，判定故障 span。

        `failing_service` 是对故障服务的推断：优先采信「业务根因」类错误，
        把 feign / Read timed out 之类视为下游调用症状（即表面现象）排除在外。
        """
        start, end = _parse_window(start_time, end_time)
        return await es.get_trace(trace_id, start, end, limit=limit)

    return get_trace


def _build_query_metrics():
    async def query_metrics(
        service: ServiceType,
        metric: Annotated[str, Field(description=_METRIC_DESC)],
        start_time: StartTime,
        end_time: EndTime,
        step_seconds: Annotated[
            int,
            Field(ge=5, le=3600, description="采样步长（秒），默认 30"),
        ] = 30,
    ) -> dict:
        """查询服务在**时间窗口**内的指标（Prometheus query_range）。

        返回窗口内聚合（峰值/均值/最小/最后）与序列点。`value` 取**峰值**——
        诊断关心的是"有没有打满"，不是平均值。

        指标为 `cpu_percent` / `memory_percent` / `disk_percent` /
        `error_rate` / `p95_latency_ms`；无数据时 `value` 为 null（例如容器未设
        内存 limit），**不要把 null 当成 0**。
        """
        start, end = _parse_window(start_time, end_time)
        return await prom.query_range(service, metric, start, end, step_seconds)

    return query_metrics


def _build_check_infra():
    async def check_infra(
        namespace: Annotated[
            str | None, Field(description="K8s namespace；不传则用服务端默认配置")
        ] = None,
        pod: Annotated[
            str | None,
            Field(description="Pod 名或服务名（按 app 标签匹配）；不传则列出该 namespace 全部 Pod"),
        ] = None,
    ) -> dict:
        """查询 Pod 状态与重启次数（K8s **当前状态**，无时间参数）。

        不传 `pod` 时返回该 namespace 的 Pod 概览列表。
        """
        return await k8s.check_infra(namespace=namespace, pod=pod)

    return check_infra


def _build_describe_pod():
    async def describe_pod(
        pod: Annotated[str, Field(description="Pod 名（完整名，含 deployment hash 与随机后缀）")],
        namespace: Annotated[
            str | None, Field(description="K8s namespace；不传则用服务端默认配置")
        ] = None,
    ) -> dict:
        """查看 Pod 详情（kubectl describe，含事件与资源水位；无时间参数）。"""
        return await k8s.describe_pod(namespace=namespace, pod=pod)

    return describe_pod


FACTORIES = {
    "query_logs": _build_query_logs,
    "get_trace": _build_get_trace,
    "query_metrics": _build_query_metrics,
    "check_infra": _build_check_infra,
    "describe_pod": _build_describe_pod,
}
