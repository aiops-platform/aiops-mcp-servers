"""Elasticsearch 后端：按**时间窗口 + 查询目标**检索应用日志，并重建调用链。

与旧直连实现的差别（design-v5.5 §2）：查询**必须**带 ``start``/``end``——
旧实现只按 ``@timestamp`` 倒序取 N 条（无窗口），诊断无法聚焦故障发生的那几分钟。

字段约定（测试床 app-logs 索引）：日志在 ``app.*`` 下，时间为 ``app.@timestamp``，
服务 ``app.service``、级别 ``app.level``、链路 ``app.traceId``（**驼峰**，非 trace_id）。
"""
from __future__ import annotations

from datetime import datetime

import httpx

from aiops_datasource_mcp_server.backends.http import fetch_json
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# 下游调用症状特征：上游服务报的错其实是「被调用方出问题」的表现，不是根因所在
_DOWNSTREAM_SYMPTOMS = (
    "feign",
    "read timed out",
    "connect timed out",
    "connection refused",
    "executing ",
    "could not connect",
)


def _time_range(start: datetime, end: datetime) -> dict:
    """ES range 过滤（含边界）。"""
    return {"range": {"app.@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}}


def _time_range_alt(start: datetime, end: datetime) -> dict:
    """兼容顶层 @timestamp（非 app.* 的日志源）。"""
    return {"range": {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}}


def _extract(hit: dict) -> dict:
    """把一条 ES hit 归一成扁平日志记录（兼容 app.* 与顶层两种布局）。"""
    src = hit.get("_source", {}) or {}
    app = src.get("app") or {}
    return {
        "@timestamp": app.get("@timestamp") or src.get("@timestamp"),
        "level": app.get("level") or src.get("level"),
        "service": app.get("service") or src.get("service"),
        "trace_id": app.get("traceId") or app.get("trace_id") or src.get("traceId"),
        "message": str(app.get("message") or src.get("message") or "")[:500],
    }


async def _search(
    body: dict, tool: str, transport: httpx.AsyncBaseTransport | None
) -> dict:
    """执行 _search，返回 ``{"hits": [...], "total": {...}, "buckets": {...}}``。

    ⚠️ **这里必须把 ``hits.total`` 与 ``aggregations`` 一起带回去。**
    早先只返回裸的 ``hits.hits``，于是 ``query_logs`` 拿 ``len(logs)`` 当"窗口内有
    多少条"——**那是"我返回了几条"，不是"命中了多少条"**。真实系统里一小时可能有
    百万条日志，agent 看到 ``total: 8`` 会得出"窗口内只有 8 条"的结论，
    而真相是"从一大堆里截了 8 条"。**没有字段能看出被截断了。**

    ``hits.total`` 缺失即报错，不用默认值兜底——猜一个数正是要修的那类缺陷。
    """
    settings = get_settings()
    url = f"{settings.datasource_es_url.rstrip('/')}/{settings.datasource_es_index}/_search"
    result = await fetch_json(url, method="POST", json_body=body, tool=tool, transport=transport)
    if not result.get("success"):
        raise AppError(ErrorCode.TOOL_EXECUTION_ERROR, str(result.get("error")))

    payload = result.get("data") or {}
    if isinstance(payload, str):
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR, f"ES 返回非 JSON（可能被截断）：{payload[:300]}"
        )

    hits_block = payload.get("hits") or {}
    total = hits_block.get("total")
    if total is None:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR,
            "ES 响应缺少 hits.total，无法给出真实命中数。"
            "本工具**不返回**用「返回条数」冒充的 total（那会让调用方以为没被截断）。",
        )
    return {
        "hits": hits_block.get("hits") or [],
        "total": total,
        "buckets": payload.get("aggregations") or {},
    }


#: ``track_total_hits`` 上限。超过它 ES 返回 ``relation="gte"``，即 total 是**下界**。
#: 必须把 relation 如实透出——把下界当精确值，正是本函数要修的那类"看着正常的错答案"。
_TRACK_TOTAL_HITS = 10_000

#: 聚合桶数上限。服务数可能上百，但调用方要的是"谁在大头"，取前 N 个足够，
#: 且**必须**把是否被截断说清楚（见返回体的 ``by_service_truncated``）。
_AGG_BUCKETS = 50


def _buckets(block: dict, agg_name: str, *, field: str) -> list[dict]:
    """把 terms 聚合的桶归一成 ``[{"<field>": …, "count": …}, …]``（已按 count 降序）。

    ``agg_name`` 是请求里给聚合起的名字（``svc`` / ``lvl``），``field`` 是**输出键名**
    （``service`` / ``level``）——两者不同：前者是 ES 查询里的标识，后者是调用方读的字段。
    """
    buckets = ((block.get(agg_name) or {}).get("buckets")) or []
    return [{field: b.get("key"), "count": b.get("doc_count", 0)} for b in buckets]


async def query_logs(
    start: datetime,
    end: datetime,
    *,
    service: str | None = None,
    level: str | None = None,
    limit: int = 50,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """按时间窗口检索日志（可选 service / level 过滤）。

    ## 「返回了几条」与「命中了多少条」是两件事

    返回体的 ``total`` 是**真实命中数**（``total_relation="gte"`` 时是下界）；
    ``returned`` 才是本次返回的条数。``by_service`` / ``by_level`` 走 **terms 聚合**，
    是**全量**分布，不受分页影响——调用方问"哪些服务在报错、各多少条"，
    一次就能拿到准确答案，不必翻页。
    """
    def _body(field_prefix: str, time_filter: dict, sort_field: str) -> dict:
        filters: list[dict] = [time_filter]
        if service:
            filters.append({"term": {f"{field_prefix}service.keyword": service}})
        if level:
            filters.append({"term": {f"{field_prefix}level.keyword": level.upper()}})
        return {
            "size": limit,
            "track_total_hits": _TRACK_TOTAL_HITS,
            "sort": [{sort_field: "desc"}],
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "svc": {"terms": {"field": f"{field_prefix}service.keyword", "size": _AGG_BUCKETS}},
                "lvl": {"terms": {"field": f"{field_prefix}level.keyword", "size": 10}},
            },
        }

    payload = await _search(_body("app.", _time_range(start, end), "app.@timestamp"),
                            "query_logs", transport)

    # 二级兜底：**app.* 布局窗口内零命中**时，试顶层字段布局（不同日志源布局不同）。
    # ⚠️ 判据是 total == 0，不能是"没有 hits"——否则"窗口内确实没数据"与
    #    "有数据但没取到"就分不开了。
    if payload["total"]["value"] == 0:
        alt = await _search(_body("", _time_range_alt(start, end), "@timestamp"),
                            "query_logs", transport)
        if alt["total"]["value"] > 0:
            payload = alt

    total_value = payload["total"]["value"]
    total_relation = payload["total"].get("relation", "eq")   # eq | gte
    logs = [_extract(h) for h in payload["hits"]]
    by_service = _buckets(payload["buckets"], "svc", field="service")
    by_level = _buckets(payload["buckets"], "lvl", field="level")

    total_txt = f"{total_value}{'+' if total_relation == 'gte' else ''}"
    dist = "、".join(f"{b['service']} {b['count']}" for b in by_service[:5])
    return {
        "total": total_value,
        #: eq = 精确；gte = 超过 track_total_hits 上限，total 是**下界**
        "total_relation": total_relation,
        "returned": len(logs),
        "has_more": len(logs) < total_value,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "filter": {"service": service, "level": level},
        #: **全量**分布（terms 聚合），不是"本页 N 条里的分布"
        "by_service": by_service,
        "by_level": by_level,
        "logs": logs,
        "summary": (
            f"窗口内命中 {total_txt} 条，本次返回 {len(logs)} 条"
            f"（{'还有更多' if len(logs) < total_value else '已全部返回'}）"
            + (f"；服务分布（全量）：{dist}" if dist else "")
        ),
    }


async def get_trace(
    trace_id: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 100,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """按 trace_id + 时间窗口重建调用链，并判定故障 span。

    故障 span 判定启发式（**测试床特定经验，非通用算法**）：
    优先「错误属于业务根因」的服务；把 feign/Read timed out 之类视为**下游调用
    症状**（是别人的错导致的表面现象），排除在根因之外。
    """
    should = [
        {"term": {"app.traceId.keyword": trace_id}},
        {"term": {"app.trace_id.keyword": trace_id}},
        {"term": {"app.traceId": trace_id}},
    ]
    body = {
        "size": min(limit, 200),
        "sort": [{"app.@timestamp": "asc"}],
        "query": {"bool": {"filter": [_time_range(start, end)], "should": should,
                           "minimum_should_match": 1}},
    }
    payload = await _search(body, "get_trace", transport)
    logs = [_extract(h) for h in payload["hits"]]

    # 按 service 聚合，构造 span 视图
    spans: dict[str, list[dict]] = {}
    for log in logs:
        spans.setdefault(log["service"] or "?", []).append(log)

    chain = []
    for svc, slogs in spans.items():
        errors = [x for x in slogs if (x["level"] or "").upper() == "ERROR"]
        chain.append({
            "service": svc,
            "spans": len(slogs),
            "has_error": bool(errors),
            # "完成/成功" 关键字是测试床日志的约定（应用会打印完成日志）
            "completed": any(
                ("完成" in x["message"] or "成功" in x["message"]) for x in slogs
            ),
            "first_error": errors[0]["message"] if errors else None,
        })

    def _is_downstream_symptom(span: dict) -> bool:
        err = (span.get("first_error") or "").lower()
        return any(k in err for k in _DOWNSTREAM_SYMPTOMS)

    errored = [s for s in chain if s["has_error"] and not s["completed"]]
    origin = [s for s in errored if not _is_downstream_symptom(s)]
    failing = (origin or errored or [{}])[0].get("service") if errored else None

    return {
        "trace_id": trace_id,
        "total": len(logs),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "chain": chain,
        "failing_service": failing,
        "logs": logs[:20],
        "summary": (
            f"trace {trace_id} 重建 {len(logs)} 条日志 / {len(chain)} 个服务"
            + (f"，故障 span 疑似 {failing}" if failing else "，未见明显故障 span")
        ),
    }
