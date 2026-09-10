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
) -> list[dict]:
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
    return ((payload.get("hits") or {}).get("hits")) or []


async def query_logs(
    start: datetime,
    end: datetime,
    *,
    service: str | None = None,
    level: str | None = None,
    limit: int = 50,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """按时间窗口检索日志（可选 service / level 过滤）。"""
    filters: list[dict] = [_time_range(start, end)]
    if service:
        filters.append({"term": {"app.service.keyword": service}})
    if level:
        filters.append({"term": {"app.level.keyword": level.upper()}})

    body = {
        "size": min(limit, 200),
        "sort": [{"app.@timestamp": "desc"}],
        "query": {"bool": {"filter": filters}},
    }
    hits = await _search(body, "query_logs", transport)

    # 二级兜底：app.* 布局无命中时，试顶层字段布局（不同日志源可能不同）
    if not hits:
        alt_filters: list[dict] = [_time_range_alt(start, end)]
        if service:
            alt_filters.append({"term": {"service.keyword": service}})
        if level:
            alt_filters.append({"term": {"level.keyword": level.upper()}})
        hits = await _search(
            {
                "size": min(limit, 200),
                "sort": [{"@timestamp": "desc"}],
                "query": {"bool": {"filter": alt_filters}},
            },
            "query_logs",
            transport,
        )

    logs = [_extract(h) for h in hits]
    by_service: dict[str, int] = {}
    for log in logs:
        by_service[log["service"] or "?"] = by_service.get(log["service"] or "?", 0) + 1

    return {
        "total": len(logs),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "filter": {"service": service, "level": level},
        "by_service": by_service,
        "logs": logs,
        "summary": (
            f"窗口内检索到 {len(logs)} 条日志"
            + (f"（服务分布 {by_service}）" if by_service else "")
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
    hits = await _search(body, "get_trace", transport)
    logs = [_extract(h) for h in hits]

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
