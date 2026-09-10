"""Prometheus 后端：PromQL 映射 + 时间窗口查询。

映射的语义正确性是本 server 的核心价值——旧直连实现因命名错配把 5 个指标
全部映射成同一条 CPU 查询，agent 拿到 5 个一模一样的数字却以为是真实测量。
"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.backends.prometheus import (
    available_metrics,
    build_query,
)
from aiops_datasource_mcp_server.errors import AppError, ErrorCode


def test_all_metrics_produce_distinct_queries() -> None:
    """5 个指标必须产生**互不相同**的 PromQL（这是本次修复的核心）。"""
    exprs = [build_query(m, "order-service.*") for m in available_metrics()]
    assert len(set(exprs)) == len(exprs), "不同 metric 不得映射到同一表达式"
    assert len(exprs) == 5


def test_each_metric_maps_to_expected_source() -> None:
    """每个指标映射到正确的数据源（防止再出现「查 error_rate 用 CPU 计数器」）。"""
    assert "container_cpu_usage_seconds_total" in build_query("cpu_percent", "svc.*")
    assert "container_memory_working_set_bytes" in build_query("memory_percent", "svc.*")
    disk = build_query("disk_percent", "svc.*")
    assert "data_disk_free_bytes" in disk and "data_disk_total_bytes" in disk
    err = build_query("error_rate", "svc.*")
    assert "http_server_requests_seconds_count" in err and "5.." in err
    lat = build_query("p95_latency_ms", "svc.*")
    assert "http_server_requests_seconds_sum" in lat


def test_unknown_metric_raises_with_available_list() -> None:
    """未知 metric 必须报错并列出可用项——不得静默兜底成 CPU。"""
    with pytest.raises(AppError) as ei:
        build_query("whatever", "svc.*")
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    msg = ei.value.message
    assert "未知 metric" in msg
    for m in available_metrics():
        assert m in msg, f"错误信息应列出可用 metric {m} 供调用方纠正"


def test_metrics_without_escape_hatch() -> None:
    """本 server 不提供 promql:/cadvisor: 透传——裸表达式一律按未知 metric 拒绝。"""
    for raw in ('promql:up{job="x"}', "cadvisor:container_cpu_usage_seconds_total"):
        with pytest.raises(AppError) as ei:
            build_query(raw, "svc.*")
        assert ei.value.code == ErrorCode.INVALID_REQUEST


def test_division_guarded_by_positive_filter() -> None:
    """分母必须 >0 过滤：未设 limit 时容器 limit=0，相除得 +Inf 会被当成真实数字。"""
    assert "> 0" in build_query("memory_percent", "svc.*")
    assert "> 0" in build_query("disk_percent", "svc.*")
    assert "> 0" in build_query("cpu_percent", "svc.*")


async def test_query_range_builds_request_and_aggregates(env) -> None:
    """query_range 请求构造 + 窗口聚合（峰值/均值/序列）。"""
    import httpx
    from aiops_datasource_mcp_server.backends.prometheus import query_range as qr

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "matrix", "result": [
                {"metric": {"pod": "order-service-1"},
                 "values": [[1789000000, "10"], [1789000030, "30"]]},
            ]},
        })

    from datetime import UTC, datetime

    out = await qr(
        "order-service",
        "cpu_percent",
        datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 7, 0, tzinfo=UTC),
        30,
        transport=httpx.MockTransport(handler),
    )

    assert seen["path"] == "/api/v1/query_range"  # 用 query_range（有窗口），非瞬时 query
    assert "start" in seen["params"] and "end" in seen["params"]
    assert seen["params"]["step"] == "30"
    assert out["value"] == 30.0     # 峰值
    assert out["min"] == 10.0
    assert out["avg"] == 20.0
    assert out["last"] == 30.0
    assert out["series"] == [[1789000000.0, 10.0], [1789000030.0, 30.0]]
    assert out["window"]["step_seconds"] == 30


async def test_query_range_filters_inf_and_nan(env) -> None:
    """Inf/NaN 不得进入聚合——否则会被当成「内存爆了」。"""
    from datetime import UTC, datetime

    import httpx
    from aiops_datasource_mcp_server.backends.prometheus import query_range as qr

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"resultType": "matrix", "result": [
                {"metric": {}, "values": [[1789000000, "NaN"], [1789000030, "+Inf"],
                                          [1789000060, "5"]]},
            ]},
        })

    out = await qr(
        "svc", "memory_percent",
        datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 7, 0, tzinfo=UTC),
        30, transport=httpx.MockTransport(handler),
    )
    assert out["value"] == 5.0
    assert out["series"] == [[1789000060.0, 5.0]]  # NaN/+Inf 已被剔除


async def test_query_range_empty_is_null_not_zero(env) -> None:
    """无数据必须为 null——0 与「没取到」是两回事，不可混淆。"""
    from datetime import UTC, datetime

    import httpx
    from aiops_datasource_mcp_server.backends.prometheus import query_range as qr

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success",
                                         "data": {"resultType": "matrix", "result": []}})

    out = await qr(
        "svc", "cpu_percent",
        datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 9, 10, 7, 0, tzinfo=UTC),
        30, transport=httpx.MockTransport(handler),
    )
    assert out["value"] is None
    assert out["min"] is None and out["avg"] is None
    assert "无数据" in out["summary"]


async def test_query_range_upstream_error_raises_apperror(env) -> None:
    """上游非 2xx → AppError（不返回半截数据当成功）。"""
    from datetime import UTC, datetime

    import httpx
    from aiops_datasource_mcp_server.backends.prometheus import query_range as qr

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(AppError) as ei:
        await qr(
            "svc", "cpu_percent",
            datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
            datetime(2026, 9, 10, 7, 0, tzinfo=UTC),
            30, transport=httpx.MockTransport(handler),
        )
    assert ei.value.code == ErrorCode.TOOL_EXECUTION_ERROR
    assert "503" in ei.value.message
