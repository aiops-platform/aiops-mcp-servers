"""上游 HTTP 调用与截断（ES / Prometheus 共用）。

两条核心约定（design-v5.5 §6）：

1. **预期失败归一为结果字典，不抛异常**——只有编程错误才抛。工具层因此无需
   到处 try/except，调用方拿到的是 ``{success: False, error: "[CODE] ..."}``。
2. **截断必须流式且有界，且显式可见**——读到字节预算即停（不先全读再切），
   在 UTF-8 码点边界回退，并追加可见后缀。静默截断会让模型拿残缺内容当完整
   事实（实测：无头 diff 被模型自行编造补齐）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

logger = logging.getLogger("aiops_datasource_mcp_server.http")

# 截断后缀：必须显式，且提示下一步动作（收窄范围），否则模型会把残缺当完整
_TRUNC_SUFFIX = "\n... (truncated at {limit} bytes, please narrow the scope)"


def _clip_utf8(data: bytes) -> bytes:
    """在 UTF-8 码点边界处截断（避免半个多字节字符变成替换符）。"""
    n = 0
    while data and 0x80 <= data[-1] <= 0xBF:  # 去掉末尾连续续字节
        data = data[:-1]
        n += 1
    if not data:
        return b""
    lead = data[-1]
    if lead < 0xC0:  # ASCII：本身就在码点边界
        return data
    need = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2
    if n + 1 >= need:  # 该多字节字符完整（含前导字节已在 data 内）
        return data
    return data[:-1]  # 不完整：前导字节一并去掉


async def fetch_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    tool: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """发起一次上游请求并按字节上限流式读取，返回归一化结果字典。

    ``transport`` 仅用于测试注入（``httpx.MockTransport``）。
    """
    settings = get_settings()
    max_bytes = settings.datasource_max_response_bytes
    started = time.perf_counter()

    chunks = bytearray()
    truncated = False
    status: int | None = None

    try:
        async with (
            httpx.AsyncClient(
                timeout=settings.datasource_request_timeout_sec,
                transport=transport,
                follow_redirects=True,  # 3xx 不可静默变空数据，伪装成"无日志"
            ) as client,
            client.stream(method, url, params=params, json=json_body) as resp,
        ):
            status = resp.status_code
            async for chunk in resp.aiter_bytes():
                room = max_bytes - len(chunks)
                if room <= 0:
                    truncated = True
                    break
                chunks += chunk[:room]
                if len(chunk) > room:
                    truncated = True
                    break
    except httpx.TimeoutException:
        took = (time.perf_counter() - started) * 1000
        logger.warning("tool=%s upstream timeout url=%s took_ms=%.0f", tool, url, took)
        return {
            "success": False,
            "tool": tool,
            "error": f"[{ErrorCode.TOOL_EXECUTION_ERROR}] 上游请求超时"
            f"（{settings.datasource_request_timeout_sec}s）：{url}",
            "took_ms": round(took, 1),
        }
    except httpx.HTTPError as exc:
        took = (time.perf_counter() - started) * 1000
        logger.warning("tool=%s upstream error url=%s err=%s", tool, url, exc)
        return {
            "success": False,
            "tool": tool,
            "error": f"[{ErrorCode.TOOL_EXECUTION_ERROR}] 上游连接失败：{exc}",
            "took_ms": round(took, 1),
        }

    took = (time.perf_counter() - started) * 1000
    text = _clip_utf8(bytes(chunks)).decode("utf-8", errors="replace")

    if status is not None and status >= 400:
        logger.warning("tool=%s upstream HTTP %s url=%s", tool, status, url)
        return {
            "success": False,
            "tool": tool,
            "error": f"[{ErrorCode.TOOL_EXECUTION_ERROR}] 上游 HTTP {status}: {text[:2000]}",
            "upstream_status": status,
            "took_ms": round(took, 1),
        }

    if truncated:
        text += _TRUNC_SUFFIX.format(limit=max_bytes)

    data: Any
    try:
        import json as _json

        data = _json.loads(text)
    except ValueError:
        # 非 JSON（如截断后的残文）→ 原样返回，不假装解析成功
        data = text

    return {
        "success": True,
        "tool": tool,
        "upstream_status": status,
        "data": data,
        "truncated": truncated,
        "took_ms": round(took, 1),
    }


def require_success(result: dict, tool: str) -> dict:
    """把归一化失败结果升格为 ``AppError``，让工具层以 MCP 错误返回。"""
    if result.get("success"):
        return result
    raise AppError(
        ErrorCode.TOOL_EXECUTION_ERROR,
        str(result.get("error") or "上游查询失败"),
        {"tool": tool},
    )
