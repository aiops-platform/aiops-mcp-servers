"""Prometheus 指标测试。"""
from __future__ import annotations

from git_mcp_server import metrics


def _reset():
    for counter in (metrics.REQUESTS_TOTAL, metrics.TOOL_ERRORS_TOTAL):
        counter.clear()


def test_observe_request_increments():
    _reset()
    timer = metrics.observe_request("get_repo_status")
    timer.stop()
    assert metrics.REQUESTS_TOTAL.labels(tool="get_repo_status")._value.get() == 1
    assert metrics.TOOL_ERRORS_TOTAL.labels(tool="get_repo_status")._value.get() == 0


def test_observe_error_increments_error_counter():
    _reset()
    timer = metrics.observe_request("blame_file")
    timer.stop(error=True)
    assert metrics.REQUESTS_TOTAL.labels(tool="blame_file")._value.get() == 1
    assert metrics.TOOL_ERRORS_TOTAL.labels(tool="blame_file")._value.get() == 1
