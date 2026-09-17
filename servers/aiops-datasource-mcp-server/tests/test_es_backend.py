"""Elasticsearch 后端：时间窗口查询 + 调用链重建。"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from aiops_datasource_mcp_server.backends import es
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

START = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
END = datetime(2026, 9, 10, 7, 0, tzinfo=UTC)


def _aggs(docs, *, services: list[tuple[str, int]] | None = None,
          levels: list[tuple[str, int]] | None = None) -> dict:
    """聚合桶。默认从文档现算——**全量**分布，与返回的 hits 无关。"""
    if services is None:
        counts: dict[str, int] = {}
        for d in docs:
            svc = (d.get("app") or {}).get("service") or d.get("service") or "?"
            counts[svc] = counts.get(svc, 0) + 1
        services = sorted(counts.items(), key=lambda kv: -kv[1])
    if levels is None:
        lc: dict[str, int] = {}
        for d in docs:
            lv = (d.get("app") or {}).get("level") or d.get("level") or "?"
            lc[lv] = lc.get(lv, 0) + 1
        levels = sorted(lc.items(), key=lambda kv: -kv[1])
    return {
        "svc": {"buckets": [{"key": k, "doc_count": v} for k, v in services]},
        "lvl": {"buckets": [{"key": k, "doc_count": v} for k, v in levels]},
    }


def _hits(*docs: dict, total: int | None = None, relation: str = "eq",
          aggs: dict | None = None) -> dict:
    """构造一个**真实形状**的 ES 响应。

    ⚠️ 早期这个 helper 只造 ``{"hits": {"hits": [...]}}``——**没有 total、没有
    aggregations**。正是这个失真的 mock，让"``total`` 其实是返回条数"这个缺陷
    一路溜过了测试：生产端把 ``hits.total`` 整个丢掉，测试端也从没造过它，
    两边谁都没发现。**mock 比被测代码更宽松，等于没测。**
    """
    return {
        "hits": {
            "total": {"value": len(docs) if total is None else total, "relation": relation},
            "hits": [{"_source": d} for d in docs],
        },
        "aggregations": aggs if aggs is not None else _aggs(docs),
    }


async def test_query_logs_always_applies_time_range(env) -> None:
    """**任何** query_logs 调用都必须带时间范围过滤（本 server 不提供无窗口查询）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=_hits())

    await es.query_logs(START, END, service="order-service",
                        transport=httpx.MockTransport(handler))

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

    out = await es.query_logs(START, END, service="warranty-service",
                              transport=httpx.MockTransport(handler))
    assert out["total"] == 1
    log = out["logs"][0]
    assert log["service"] == "warranty-service"
    assert log["level"] == "ERROR"
    assert log["trace_id"] == "tr-1"  # 驼峰 traceId → 归一为 trace_id
    assert out["by_service"] == [{"service": "warranty-service", "count": 1}]
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
        await es.query_logs(START, END, service="order-service",
                            transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.TOOL_EXECUTION_ERROR
    assert "500" in ei.value.message


# ======================================================================
# 分页与"返回了几条 ≠ 命中了多少条"
#
# 这一组针对一个真实缺陷：_search 早先只把 hits.hits 带回来，hits.total 被整个丢掉，
# 于是 query_logs 拿 len(logs) 当"窗口内有多少条"。真实系统里一小时可能有百万条，
# agent 看到 total: 8 会得出"窗口内只有 8 条"，而真相是"从一大堆里截了 8 条"。
# 更糟的是**返回体里没有任何字段能看出被截断了**。
# ======================================================================
async def test_total_is_real_hit_count_not_returned_count(env) -> None:
    """`total` 必须是**真实命中数**，不是本次返回的条数。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hits(
            {"app": {"service": "order-service", "level": "ERROR", "message": "a"}},
            {"app": {"service": "order-service", "level": "ERROR", "message": "b"}},
            {"app": {"service": "order-service", "level": "ERROR", "message": "c"}},
            total=812_340,   # 窗口内真实命中
        ))

    out = await es.query_logs(START, END, level="ERROR",
                              transport=httpx.MockTransport(handler))
    assert out["total"] == 812_340, "total 被当成返回条数了——这正是要修的缺陷"
    assert out["returned"] == 3
    assert out["has_more"] is True
    assert "812340" in out["summary"]


async def test_total_relation_gte_is_surfaced(env) -> None:
    """超过 track_total_hits 上限时 ES 给的是**下界**，必须如实透出，不能当精确值。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hits(
            {"app": {"service": "order-service", "level": "ERROR", "message": "a"}},
            total=10000, relation="gte",
        ))

    out = await es.query_logs(START, END, level="ERROR",
                              transport=httpx.MockTransport(handler))
    assert out["total_relation"] == "gte"
    assert "10000+" in out["summary"], "下界必须写成 10000+，不能写成精确的 10000"


async def test_by_service_is_full_distribution_not_page_distribution(env) -> None:
    """`by_service` 走 terms 聚合，是**全量**分布——不受本页条数影响。

    早先是遍历返回的几十条现数，于是"服务分布"其实是"前 N 条里的分布"：
    不在前 N 条里的服务会被系统性漏掉，而调用方毫不知情。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # 本页只返回 1 条，但聚合说窗口内有三个服务
        return httpx.Response(200, json=_hits(
            {"app": {"service": "order-service", "level": "ERROR", "message": "x"}},
            total=500,
            aggs=_aggs([], services=[("order-service", 498), ("gateway-service", 2)]),
        ))

    out = await es.query_logs(START, END, level="ERROR",
                              transport=httpx.MockTransport(handler))
    assert out["by_service"] == [
        {"service": "order-service", "count": 498},
        {"service": "gateway-service", "count": 2},
    ], "by_service 必须是聚合出来的全量分布"
    assert out["returned"] == 1


async def test_missing_hits_total_errors_instead_of_guessing(env) -> None:
    """ES 没给 total 时**报错**，不用默认值兜底——猜一个数正是要修的那类缺陷。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}})   # 没有 total

    with pytest.raises(AppError) as ei:
        await es.query_logs(START, END, service="order-service",
                            transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.TOOL_EXECUTION_ERROR
    assert "total" in ei.value.message


# ======================================================================
# 选择性条件与分页边界
# ======================================================================
async def test_time_only_query_is_rejected(env) -> None:
    """**只给时间区间**必须 fail-closed。

    「时间区间」是**范围**不是**选择**——它不缩小结果集，只圈定"哪一段"。
    真实系统里一小时也能有 GB 级日志，放开等于要求全量扫描。
    """
    def handler(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("不该发出请求——应当在入口就拒绝")

    with pytest.raises(AppError) as ei:
        await es.query_logs(START, END, transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "service" in ei.value.message and "level" in ei.value.message


async def test_pagination_offset_is_applied(env) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json=_hits(total=100))

    await es.query_logs(START, END, level="ERROR", offset=40, limit=10,
                        transport=httpx.MockTransport(handler))
    assert seen["body"]["from"] == 40
    assert seen["body"]["size"] == 10
    assert seen["body"]["track_total_hits"] == es._TRACK_TOTAL_HITS


async def test_deep_pagination_is_rejected(env) -> None:
    """offset+limit 超过 ES 的 max_result_window → 报错并给可操作的指引。

    不挡的话 ES 会返回一个难懂的错误；更糟的是调用方可能以为"翻到底了"。
    """
    def handler(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("不该发出请求")

    with pytest.raises(AppError) as ei:
        await es.query_logs(START, END, level="ERROR", offset=9_990, limit=50,
                            transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "10000" in ei.value.message


async def test_fallback_triggers_on_zero_total_not_empty_page(env) -> None:
    """布局兜底判据是 **total == 0**，不是"本页没有 hits"。

    用后者的话，第 2 页（本页空但窗口内有数据）会误触发兜底，
    把顶层布局的结果当成答案返回——一个安静的错答案。
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.read())
        calls.append(body)
        if "app.@timestamp" in json.dumps(body):                  # app.* 布局
            return httpx.Response(200, json=_hits(total=100))     # 有数据，只是本页为空
        return httpx.Response(200, json=_hits(total=999))         # 顶层布局（不该被采用）

    out = await es.query_logs(START, END, level="ERROR", offset=90, limit=10,
                              transport=httpx.MockTransport(handler))
    assert len(calls) == 1, "total>0 时不该再去试顶层布局"
    assert out["total"] == 100


async def test_by_logger_is_full_aggregation(env) -> None:
    """`by_logger` 走 terms 聚合，是**全量**分布——用来判断"错误来自哪段代码"。

    实测它能分开「业务代码抛的」与「容器/Servlet 包装的」。**但它只是失败模式的近似**：
    同一个失败会被劈成两组（两条的 stack_trace 首行其实相同），所以描述里写明了
    "归并请自己判断"，不冒充精确的模式划分。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hits(
            {"app": {"service": "order-service", "level": "ERROR", "message": "x"}},
            total=16,
            aggs={
                "svc": {"buckets": [{"key": "order-service", "doc_count": 16}]},
                "lvl": {"buckets": [{"key": "ERROR", "doc_count": 16}]},
                "lgr": {"buckets": [
                    {"key": "com.company.order.service.QuotationService", "doc_count": 8},
                    {"key": "org.apache.catalina.core.ContainerBase.dispatcherServlet",
                     "doc_count": 8},
                ]},
            },
        ))

    out = await es.query_logs(START, END, level="ERROR",
                              transport=httpx.MockTransport(handler))
    assert out["by_logger"] == [
        {"logger": "com.company.order.service.QuotationService", "count": 8},
        {"logger": "org.apache.catalina.core.ContainerBase.dispatcherServlet", "count": 8},
    ]
