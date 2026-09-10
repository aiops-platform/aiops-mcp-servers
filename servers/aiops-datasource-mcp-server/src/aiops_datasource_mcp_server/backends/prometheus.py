"""Prometheus 后端：领域语义 metric → PromQL 映射 + query_range 时间窗口查询。

**映射住在这里，不透传给 LLM**（design-v5.5 §5）：调用方传 ``cpu_percent``，
不传 PromQL 表达式。这是本 server 与 applog（声明式透传）最本质的区别。

三条从实测中得来的硬约定（design-v5.5 §2/§8）：

1. **未知 metric 必须报错，不得静默兜底**。旧实现 ``if metric in queries … else
   默认 CPU``，而 LLM 按 MetricsEvidenceSchema 传的 5 个名字全部不在白名单里 →
   五个指标返回同一条 CPU 表达式的结果。**真实数据 + 错误查询比假数据更危险**：
   数字看着可信，语义完全错位。
2. **分母要过滤 > 0**。容器未设 memory limit 时 ``limit=0``，相除得 ``+Inf``；
   agent 会把 ``+Inf`` 当成"内存爆了"。同理 ``data_disk_total_bytes`` 缺失时得
   ``NaN``。过滤后退化为"无数据"，诚实得多。
3. **返回聚合值 + 序列**：聚合值供 MetricsEvidenceSchema 字段直接落位，
   序列供判断趋势。
"""
from __future__ import annotations

from datetime import datetime

import httpx

from aiops_datasource_mcp_server.backends.http import fetch_json
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# 容器 CPU 用量（cores）；与 _cpu_limit 相除得占用百分比
_CPU_CORES = "sum(rate(container_cpu_usage_seconds_total{{{sel}}}[{window}]))"


def _sel(pod_re: str) -> str:
    return f'pod=~"{pod_re}",container!="POD"'


def build_query(metric: str, pod_re: str, *, window: str = "5m") -> str:
    """领域语义 metric → PromQL 表达式。

    键名与 agentflow 的 ``MetricsEvidenceSchema`` 字段一一对应
    （cpu_percent / memory_percent / disk_percent / error_rate / p95_latency_ms）。
    """
    sel = _sel(pod_re)
    cpu_limit = (
        f"sum(container_spec_cpu_quota{{{sel}}}/container_spec_cpu_period{{{sel}}})"
    )
    queries = {
        "cpu_percent": f"100 * {_CPU_CORES.format(sel=sel, window=window)} / ({cpu_limit} > 0)",
        "memory_percent": (
            f"100 * sum(container_memory_working_set_bytes{{{sel}}})"
            f" / (sum(container_spec_memory_limit_bytes{{{sel}}}) > 0)"
        ),
        # 应用侧 data_disk_*（testbed 的 /data 盘）；total=0/NaN 时过滤为「无数据」
        "disk_percent": (
            f'100 * (1 - sum(data_disk_free_bytes{{service=~"{pod_re}"}})'
            f' / (sum(data_disk_total_bytes{{service=~"{pod_re}"}}) > 0))'
        ),
        # 5xx 占该服务总请求的比例（应用侧 http_server_requests_*，带 service 标签）
        "error_rate": (
            f'100 * sum(rate(http_server_requests_seconds_count{{service=~"{pod_re}",'
            f'status=~"5.."}}[{window}]))'
            f' / sum(rate(http_server_requests_seconds_count{{service=~"{pod_re}"}}[{window}]))'
        ),
        # Spring 未开 histogram 时 P95 无法算，退化为窗口内平均延迟（仍是真实数据，非编造）
        "p95_latency_ms": (
            f"1000 * sum(rate(http_server_requests_seconds_sum{{service=~\"{pod_re}\"}}[{window}]))"
            f" / sum(rate(http_server_requests_seconds_count{{service=~\"{pod_re}\"}}[{window}]))"
        ),
    }
    if metric in queries:
        return queries[metric]

    raise AppError(
        ErrorCode.INVALID_REQUEST,
        f"未知 metric {metric!r}——本 server 不提供 PromQL 透传，也不做静默兜底"
        f"（兜底会让不同指标返回同一个数字，看起来却完全可信）。"
        f"可用：{', '.join(sorted(queries))}",
        {"metric": metric, "available": sorted(queries)},
    )


def available_metrics() -> list[str]:
    """可用 metric 名（供工具描述与文档生成）。"""
    return ["cpu_percent", "memory_percent", "disk_percent", "error_rate", "p95_latency_ms"]


def _aggregate(values: list[float]) -> dict:
    """窗口内聚合：max 为默认取值（异常检测关心峰值），另给 min/avg/last。"""
    if not values:
        return {"value": None, "min": None, "max": None, "avg": None, "last": None}
    return {
        "value": max(values),  # 默认取峰值——诊断关心「是否打满」而非均值
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 6),
        "last": values[-1],
    }


async def query_range(
    service: str,
    metric: str,
    start: datetime,
    end: datetime,
    step_seconds: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """查询某服务在时间窗口内的指标（Prometheus /api/v1/query_range）。

    ``start``/``end`` 由工具层校验后传入（已保证 start < end 且跨度合规）。
    """
    settings = get_settings()
    pod_re = f"{service}.*"
    # rate() 的窗口取「与查询区间匹配但不短于 1m」，避免区间很短时 rate 无数据
    window = f"{max(step_seconds * 2, 60)}s"
    expr = build_query(metric, pod_re, window=window)

    result = await fetch_json(
        f"{settings.datasource_prom_url.rstrip('/')}/api/v1/query_range",
        params={
            "query": expr,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step_seconds,
        },
        tool="query_metrics",
        transport=transport,
    )
    if not result.get("success"):
        raise AppError(ErrorCode.TOOL_EXECUTION_ERROR, str(result.get("error")))

    payload = result.get("data") or {}
    if payload.get("status") == "error":
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR,
            f"Prometheus 报错：{payload.get('error') or payload.get('errorType')}",
        )

    series_raw = (payload.get("data") or {}).get("result") or []
    # 多条序列（多 pod）时按时间戳合并取值：同一时刻求和，得到服务级总量
    merged: dict[float, float] = {}
    for s in series_raw:
        for ts, val in s.get("values") or []:
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            # Inf/NaN 不进入聚合——否则会被当成真实数字（如「内存爆了」）
            if v != v or v in (float("inf"), float("-inf")):
                continue
            merged[float(ts)] = merged.get(float(ts), 0.0) + v

    points = sorted(merged.items())
    agg = _aggregate([v for _, v in points])

    # 无数据时给出归因提示：agent 拿到 null 若不知原因，可能误判为「指标为 0」。
    # 常见三种：未设 limit（分母为 0，百分比无定义）/ 未暴露该指标 / 窗口内无采集点。
    desc = (
        "无数据（可能原因：容器未设 limit 致分比为 0、应用未暴露该指标、"
        "或窗口内无采集点）——**不要当成 0**"
        if agg["value"] is None
        else f"峰值={agg['value']:.2f}（均值 {agg['avg']:.2f}）"
    )

    return {
        "metric": metric,
        "service": service,
        "expr": expr,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step_seconds": step_seconds,
        },
        "series_count": len(series_raw),
        **agg,
        # 序列点数上限 120（够画趋势，不撑爆返回体）
        "series": [[ts, round(v, 6)] for ts, v in points[:120]],
        "summary": f"{service} {metric} 在 {start.isoformat()}~{end.isoformat()} 内{desc}",
    }
