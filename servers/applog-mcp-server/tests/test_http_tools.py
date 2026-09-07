"""HTTP 工具逻辑测试：参数映射、透传、4xx/超时归一、截断。

用 httpx.MockTransport 注入，不碰真实网络。
"""
from __future__ import annotations

import json
import textwrap

import httpx
import pytest
from applog_mcp_server.tools.factory import build_tool
from applog_mcp_server.tools.loader import load_tool_specs

YAML_GET = textwrap.dedent(
    """\
    tools:
      - name: query_app_logs
        description: 按时间范围查询
        http:
          method: GET
          path: /api/sip-aiops/app-log
          base_url: http://upstream.test
        inputs:
          - {name: startTime, in: query, required: true, description: 开始}
          - {name: endTime, in: query, required: true, description: 结束}
          - {name: logLevel, in: query, description: 级别}
          - {name: serviceName, in: query, description: 服务}
        response: {mode: passthrough}
    """
)

YAML_POST = textwrap.dedent(
    """\
    tools:
      - name: query_by_payload
        description: POST json 查询
        http:
          method: POST
          path: /api/search
          base_url: http://upstream.test
        inputs:
          - {name: traceId, in: query, required: true, description: 链路}
          - {name: filters, in: body, required: true, description: 过滤体}
        response: {mode: passthrough}
    """
)


def _build_first(env, yaml_body):
    env.write_text(yaml_body, encoding="utf-8")
    return build_tool(load_tool_specs()[0])


def _with_transport(env, yaml_body, handler):
    env.write_text(yaml_body, encoding="utf-8")
    spec = load_tool_specs()[0]
    return build_tool(spec, transport=httpx.MockTransport(handler))


def test_get_maps_query_params_and_passthrough(env):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [{"level": "ERROR", "msg": "boom"}]})

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(
        startTime="2026-08-28T03:14:41",
        endTime="2026-08-28T10:30:00",
        logLevel="ERROR",
        serviceName="sip-aiops-management",
    )
    assert seen["method"] == "GET"
    assert seen["params"]["startTime"] == "2026-08-28T03:14:41"
    assert seen["params"]["logLevel"] == "ERROR"
    assert res["success"] is True
    assert res["data"] == {"items": [{"level": "ERROR", "msg": "boom"}]}


def test_optional_params_omitted_not_sent(env):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[1, 2, 3])

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert set(seen["params"]) == {"startTime", "endTime"}
    assert res["success"] is True
    assert res["total"] == 3  # 根是数组 -> total=len


def test_post_json_body_and_query(env):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.read() or b"{}")
        return httpx.Response(200, json={"ok": True})

    fn = _with_transport(env, YAML_POST, handler)
    # v1 入参一律 string：真实 MCP 通道只能传字符串，body 里是 { filters: "<string>" }
    res = fn(traceId="abc123", filters='{"level": "ERROR"}')
    assert seen["method"] == "POST"
    assert seen["params"] == {"traceId": "abc123"}
    assert seen["body"] == {"filters": '{"level": "ERROR"}'}
    assert res["success"] is True


def test_upstream_500_returns_failure(env):
    def handler(request):
        return httpx.Response(500, text="backend exploded")

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert res["success"] is False
    assert res["upstream_status"] == 500
    assert "backend exploded" in res["error"]


def test_upstream_timeout_returns_failure(env):
    def handler(request):
        raise httpx.ReadTimeout("took too long")

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert res["success"] is False
    assert "timeout" in res["error"]


def test_response_byte_cap_truncates(env):
    big = b'{"data": "' + b"a" * 5000 + b'"}'

    def handler(request):
        return httpx.Response(200, content=big)

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert res["success"] is True
    assert res["truncated"] is True
    assert "truncated at 1024 bytes" in res["data"]


def test_3xx_redirect_followed_to_success(env):
    """3xx 就近跟随（http→https 迁移场景），不会静默返回空 data 冒充“无日志”。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sip-aiops/app-log":
            return httpx.Response(
                302, headers={"Location": "http://upstream.test/api/next"}
            )
        return httpx.Response(200, json={"ok": True})

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert res["success"] is True
    assert res["data"] == {"ok": True}


def test_truncation_keeps_codepoint_boundary(env):
    """超限截断不把多字节字符切出 U+FFFD（码点边界对齐）。"""
    payload = '{"data": "' + "中" * 400 + '"}'

    def handler(request):
        return httpx.Response(200, content=payload.encode("utf-8"))

    fn = _with_transport(env, YAML_GET, handler)
    res = fn(startTime="t0", endTime="t1")
    assert res["truncated"] is True
    assert "\ufffd" not in res["data"]
    assert "truncated at 1024 bytes" in res["data"]


YAML_CHAIN = textwrap.dedent(
    """\
    tools:
      - name: query_chain
        description: 按 requestId 查询链路日志
        http:
          method: GET
          path: /api/sip-aiops/app-log/chain/{requestId}
          base_url: http://upstream.test
        inputs:
          - {name: requestId, in: path, required: true, description: 链路 ID}
          - {name: startTime, in: query, required: true, description: 开始}
          - {name: endTime, in: query, required: true, description: 结束}
          - {name: serviceName, in: query, description: 服务}
        response: {mode: passthrough}
    """
)


def test_path_param_substituted_into_url(env):
    seen: dict = {}
    rid = "f920ac26e3fe4c5591a7fa2e106cf37c"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"level": "ERROR"}])

    fn = _with_transport(env, YAML_CHAIN, handler)
    res = fn(requestId=rid, startTime="t0", endTime="t1", serviceName="sip-aiops")
    assert res["success"] is True
    assert seen["url"].startswith(
        "http://upstream.test/api/sip-aiops/app-log/chain/" + rid
    )
    assert "startTime=t0" in seen["url"]
    assert "serviceName=sip-aiops" in seen["url"]


def test_path_param_is_url_quoted(env):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    fn = _with_transport(env, YAML_CHAIN, handler)
    res = fn(requestId="ab cd/ef", startTime="t0", endTime="t1")
    assert res["success"] is True
    assert "/chain/ab%20cd%2Fef" in seen["url"]


def test_required_param_missing_raises_typeerror(env):
    fn = _build_first(env, YAML_GET)
    with pytest.raises(TypeError):
        fn(startTime="t0")  # endTime 必填缺失
