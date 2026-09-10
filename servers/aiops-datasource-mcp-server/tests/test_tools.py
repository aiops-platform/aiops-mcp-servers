"""工具层：时间区间 fail-closed 校验（本 server 最核心的对外契约）。"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.errors import AppError, ErrorCode
from aiops_datasource_mcp_server.tools.datasource import (
    FACTORIES,
    _parse_window,
)


def test_all_five_tools_registered() -> None:
    assert set(FACTORIES) == {
        "query_logs", "get_trace", "query_metrics", "check_infra", "describe_pod"
    }


def test_valid_window_parsed_utc() -> None:
    start, end = _parse_window("2026-09-10T06:00:00", "2026-09-10T07:00:00")
    assert start.isoformat() == "2026-09-10T06:00:00+00:00"  # naive 视为 UTC
    assert end > start


def test_z_suffix_accepted() -> None:
    start, _ = _parse_window("2026-09-10T06:00:00Z", "2026-09-10T07:00:00Z")
    assert start.hour == 6


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-45T99:99:99", ""])
def test_invalid_time_format_rejected(bad: str) -> None:
    """非法时间格式必须报错——不做宽容解析、不静默取默认值。"""
    with pytest.raises(AppError) as ei:
        _parse_window(bad, "2026-09-10T07:00:00")
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "ISO8601" in ei.value.message


def test_start_after_end_rejected() -> None:
    with pytest.raises(AppError) as ei:
        _parse_window("2026-09-10T08:00:00", "2026-09-10T07:00:00")
    assert "必须早于" in ei.value.message


def test_equal_start_end_rejected() -> None:
    with pytest.raises(AppError):
        _parse_window("2026-09-10T07:00:00", "2026-09-10T07:00:00")


def test_window_exceeding_max_span_rejected() -> None:
    """跨度超上限拒绝——防全量扫描拖垮数据源。"""
    with pytest.raises(AppError) as ei:
        _parse_window("2026-09-01T00:00:00", "2026-09-10T00:00:00")  # 9 天 > 24h
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "超过上限" in ei.value.message
    assert "收窄" in ei.value.message  # 给出可操作提示


async def test_query_logs_tool_rejects_bad_window_before_network(env) -> None:
    """时间非法时，在发请求**之前**就失败（不产生任何上游调用）。"""
    fn = FACTORIES["query_logs"]()
    with pytest.raises(AppError):
        await fn(start_time="bad", end_time="also-bad")


async def test_query_metrics_tool_rejects_unknown_metric(env) -> None:
    """未知 metric 在进 backend 前即失败，且错误含可用清单。"""
    fn = FACTORIES["query_metrics"]()
    with pytest.raises(AppError) as ei:
        await fn(
            service="order-service", metric="nope",
            start_time="2026-09-10T06:00:00", end_time="2026-09-10T07:00:00",
        )
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "cpu_percent" in ei.value.message


async def test_describe_pod_requires_pod_argument(env) -> None:
    """describe_pod 签名的 pod 是必填（由 FastMCP 层校验，此处验证签名本身）。"""
    import inspect

    fn = FACTORIES["describe_pod"]()
    params = inspect.signature(fn).parameters
    assert params["pod"].default is inspect.Parameter.empty  # 无默认值 = 必填
    assert params["namespace"].default is None              # 可选
