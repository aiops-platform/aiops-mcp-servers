"""上游 HTTP 调用封装（UpstreamClient）。

职责：组 URL、超时、非 2xx 归一、JSON 通用透传、输出字节上限截断。
所有可预期的失败都归一为结果 dict（success=False + error），不向上抛（除编程错误）。
transport 参数用于测试注入 httpx.MockTransport，缺省走真实网络。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from applog_mcp_server.config import get_settings
from applog_mcp_server.errors import AppError, ErrorCode

logger = logging.getLogger("applog_mcp_server.http")

_TRUNC_SUFFIX = "\n... (truncated at {limit} bytes, please narrow the scope)"


class UpstreamClient:
    """对单个上游 HTTP 接口执行请求并返回统一结果 dict。"""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        settings = get_settings()
        self._timeout = settings.applog_request_timeout_sec
        self._max_bytes = settings.applog_max_response_bytes
        self._transport = transport

    # -- 对外主入口 -------------------------------------------------------
    def execute(
        self,
        *,
        tool_name: str,
        method: str,
        base_url: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """执行请求并返回统一结果 dict（成功/失败均已归一）。

        所有可预期失败（超时 / 连接失败 / 非 2xx）都归一为 success=False 结果并记 warning 日志，
        不上抛；网络读取按字节上限流式截流，避免大响应整包缓冲。
        """
        url = self._build_url(base_url, path)
        started = time.perf_counter()
        try:
            status_code, content, truncated = self._request(
                method, url, params=params or None, json_body=body or None, headers=headers
            )
        except AppError as exc:
            took_ms = self._took(started)
            logger.warning(
                "tool=%s method=%s url=%s failed (took %sms): [%s] %s",
                tool_name, method, url, took_ms, exc.code, exc.message,
            )
            return {
                "success": False,
                "tool": tool_name,
                "error": f"[{exc.code}] {exc.message}",
            }
        took_ms = self._took(started)

        if not (200 <= status_code < 300):
            text = content.decode("utf-8", errors="replace")
            snippet = text[:2000] + (" ...(error body truncated)" if len(text) > 2000 else "")
            logger.warning(
                "tool=%s method=%s url=%s -> HTTP %s (took %sms)",
                tool_name, method, url, status_code, took_ms,
            )
            return {
                "success": False,
                "tool": tool_name,
                "error": (
                    f"[{ErrorCode.TOOL_EXECUTION_ERROR}] upstream HTTP {status_code}: {snippet}"
                ),
                "upstream_status": status_code,
                "truncated": truncated,
                "took_ms": took_ms,
            }

        data, total = self._parse(self._decode_payload(content, truncated))
        logger.debug(
            "tool=%s method=%s url=%s -> HTTP %s ok (took %sms, truncated=%s, total=%s)",
            tool_name, method, url, status_code, took_ms, truncated, total,
        )
        return {
            "success": True,
            "tool": tool_name,
            "upstream_status": status_code,
            "data": data,
            "total": total,
            "truncated": truncated,
            "took_ms": took_ms,
        }

    # -- 内部实现 ---------------------------------------------------------
    def _build_url(self, base_url: str, path: str) -> str:
        base = (base_url or "").rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> tuple[int, bytes, bool]:
        """流式读取上游响应，超过字节上限即中断（返回 truncated=True）。

        返回 (status_code, content_bytes, truncated)。content 恒 <= 上限，杜绝整包缓冲；
        follow_redirects=True 让 3xx 就近跟随，返回的必为最终响应（重定向往返由 httpx 处理）。
        """
        max_bytes = self._max_bytes
        status_code = 0
        chunks = bytearray()
        truncated = False
        try:
            with (
                httpx.Client(timeout=self._timeout, transport=self._transport) as client,
                client.stream(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    follow_redirects=True,
                ) as resp,
            ):
                status_code = resp.status_code
                for chunk in resp.iter_bytes():
                    room = max_bytes - len(chunks)
                    if room <= 0:
                        truncated = True
                        break
                    chunks += chunk[:room]
                    if len(chunk) > room:
                        truncated = True
                        break
        except httpx.TimeoutException as exc:
            raise AppError(
                ErrorCode.TOOL_EXECUTION_ERROR,
                f"upstream request timeout after {self._timeout}s: {method} {url}",
            ) from exc
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.TOOL_EXECUTION_ERROR,
                f"upstream request failed ({method} {url}): {exc}",
            ) from exc
        return status_code, bytes(chunks), truncated

    @staticmethod
    def _took(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    def _decode_payload(self, content: bytes, truncated: bool) -> str:
        """解码到码点边界的安全文本；截断时追加提示后缀。"""
        if truncated:
            content = self._clip_utf8(content)
        text = content.decode("utf-8", errors="replace")
        if truncated:
            text = text + _TRUNC_SUFFIX.format(limit=self._max_bytes)
        return text

    @staticmethod
    def _clip_utf8(content: bytes) -> bytes:
        """去掉尾部被切断的多字节序列，避免解码时在截断处插入 U+FFFD 破坏 JSON。"""
        data = content
        n = 0
        while data and 0x80 <= data[-1] <= 0xBF:  # 去掉末尾连续续字节
            data = data[:-1]
            n += 1
        if not data:
            return b""
        lead = data[-1]
        if lead < 0xC0:
            return data  # ASCII：本身在码点边界
        need = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2
        if n + 1 >= need:
            return content  # 该多字节字符完整（截断发生在其后）
        return data[:-1]  # 前导字节一并去掉，保证是合法前缀

    @staticmethod
    def _parse(text: str) -> tuple[Any, int | None]:
        """通用透传解析：能解析 JSON 就返回对象/数组；否则把原文当 data。"""
        try:
            data = json.loads(text)
        except ValueError:
            return text, None
        if isinstance(data, list):
            return data, len(data)
        return data, None
