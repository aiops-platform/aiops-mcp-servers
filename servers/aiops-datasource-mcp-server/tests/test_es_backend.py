"""Elasticsearch 后端：时间窗口查询 + 调用链重建。"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from aiops_datasource_mcp_server.backends import es
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

START = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
END = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)


def _hits(*docs: dict) -> dict:
    return {"hits": {"hits": [{"_source": d} for d in docs]}}


async def test_query_logs_always_applies_time_range(env) -> None:
    """**任何** query_logs 调用都必须带时间范围过滤（本 server 不提供无窗口查询）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=_hits())

    await es.query_logs(START, END, transport=httpx.MockTransport(handler))

    filters = seen["body"]["query"]["bool"]["filter"]
    assert any("range" in f for f in filters), "必须包含时间范围过滤"


async def test_query_logs_filters_by_service_and_level(env) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=_hits())

    await es.query_logs(
        START, END, service="warranty-service", level="error",
        transport=httpx.MockTransport(handler),
    )
    filters = seen["body"]["query"]["bool"]["filter"]
    flat = str(filters)
    assert "warranty-service" in flat
    assert "ERROR" in flat  # level 归一为大写


async def test_query_logs_normalizes_hits(env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hits(
            {"app": {"@timestamp": "t1", "level": "ERROR", "service": "warranty-service",
                     "traceId": "tr-1", "message": "必填参数 fin 没有传"}},
        ))

    out = await es.query_logs(START, END, transport=httpx.MockTransport(handler))
    assert out["total"] == 1
    log = out["logs"][0]
    assert log["service"] == "warranty-service"
    assert log["level"] == "ERROR"
    assert log["trace_id"] == "tr-1"  # 驼峰 traceId → 归一为 trace_id
    assert out["by_service"] == {"warranty-service": 1}
    assert out["window"]["start"] == START.isoformat()


async def test_get_trace_prefers_root_cause_over_downstream_symptom(env) -> None:
    """故障 span 判定：order-service 报 Feign 超时是**下游症状**，根因在 warranty-service。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hits(
            {"app": {"@timestamp": "t1", "level": "ERROR", "service": "order-service",
                     "message": "feign.RetryableException: Read timed out"}},
            {"app": {"@timestamp": "t2", "level": "ERROR", "service": "warranty-service",
                     "message": "查询三包期失败: IllegalArgumentException 必填参数 fin 没有传"}},
        ))

    out = await es.get_trace("tr-1", START, END, transport=httpx.MockTransport(handler))
    assert out["failing_service"] == "warranty-service"
    assert {s["service"] for s in out["chain"]} == {"order-service", "warranty-service"}
    assert "warranty-service" in out["summary"]


async def test_get_trace_searches_by_trace_id(env) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=_hits())

    await es.get_trace("abc123", START, END, transport=httpx.MockTransport(handler))
    assert "abc123" in str(seen["body"]["query"])
    assert any("range" in str(f) for f in seen["body"]["query"]["bool"]["filter"])


async def test_es_upstream_error_raises_apperror(env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="es exploded")

    with pytest.raises(AppError) as ei:
        await es.query_logs(START, END, transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.TOOL_EXECUTION_ERROR
    assert "500" in ei.value.message
