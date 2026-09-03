"""Prometheus 指标（prometheus-client）。

指标：
- git_mcp_requests_total（标签: tool）  —— 工具调用次数
- git_mcp_request_duration_seconds      —— 工具调用耗时
- git_mcp_tool_errors_total（标签: tool）—— 工具调用失败次数
- git_mcp_git_command_total             —— 底层 git 子进程执行次数
"""
from __future__ import annotations

import time

from prometheus_client import Counter, Histogram

# 工具调用
REQUESTS_TOTAL = Counter(
    "git_mcp_requests_total",
    "Total tool requests received",
    ["tool"],
)
REQUEST_DURATION = Histogram(
    "git_mcp_request_duration_seconds",
    "Tool request duration in seconds",
    ["tool"],
)
TOOL_ERRORS_TOTAL = Counter(
    "git_mcp_tool_errors_total",
    "Total tool errors raised",
    ["tool"],
)

# git 子进程
GIT_COMMAND_TOTAL = Counter(
    "git_mcp_git_command_total",
    "Total git subprocess executions",
    ["tool"],
)
GIT_COMMAND_DURATION = Histogram(
    "git_mcp_git_command_duration_seconds",
    "git subprocess duration in seconds",
    ["tool"],
)

def observe_request(tool: str) -> _RequestTimer:
    return _RequestTimer(tool)


class _RequestTimer:
    def __init__(self, tool: str):
        self.tool = tool
        self.start = time.perf_counter()

    def stop(self, *, error: bool = False) -> None:
        duration = time.perf_counter() - self.start
        REQUESTS_TOTAL.labels(tool=self.tool).inc()
        REQUEST_DURATION.labels(tool=self.tool).observe(duration)
        if error:
            TOOL_ERRORS_TOTAL.labels(tool=self.tool).inc()
