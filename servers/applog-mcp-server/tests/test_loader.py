"""tools.yaml 解析与 fail-closed 校验。"""
from __future__ import annotations

import textwrap

import pytest
from applog_mcp_server.errors import AppError, ErrorCode
from applog_mcp_server.tools.loader import load_tool_specs


def _write(tmp_path, body: str):
    p = tmp_path / "tools.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_load_single_tool(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: query_app_logs
            description: hello
            http:
              method: get
              path: /api/app-log
            inputs:
              - {name: startTime, in: query, required: true}
              - {name: logLevel, in: query}
        """,
    )
    specs = load_tool_specs(str(p))
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "query_app_logs"
    assert spec.http.method == "GET"  # 小写被归一为 GET
    assert spec.http.base_url is None  # 缺省回落 default
    assert spec.description == "hello"
    by_name = {i.name: i for i in spec.inputs}
    assert by_name["startTime"].required is True
    assert by_name["startTime"].location == "query"
    assert by_name["logLevel"].required is False


def test_load_multiple_tools(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - {name: query_a, http: {method: GET, path: /a}}
          - {name: query_b, http: {method: POST, path: /b}}
        """,
    )
    assert {s.name for s in load_tool_specs(str(p))} == {"query_a", "query_b"}


def test_missing_file(env, tmp_path):
    with pytest.raises(AppError) as ei:
        load_tool_specs(str(tmp_path / "nope.yaml"))
    assert ei.value.code == ErrorCode.CONFIG_ERROR


def test_empty_tools_rejected(env, tmp_path):
    p = _write(tmp_path, "tools: []\n")
    with pytest.raises(AppError, match="未声明任何 tool"):
        load_tool_specs(str(p))


def test_duplicate_tool_name(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - {name: q, http: {method: GET, path: /a}}
          - {name: q, http: {method: GET, path: /b}}
        """,
    )
    with pytest.raises(AppError, match="名字重复"):
        load_tool_specs(str(p))


def test_unknown_field_forbidden(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            typo_field: 1
        """,
    )
    with pytest.raises(AppError, match="定义非法"):
        load_tool_specs(str(p))


def test_body_input_requires_post(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            inputs:
              - {name: payload, in: body}
        """,
    )
    with pytest.raises(AppError, match="in=body 仅支持 method=POST"):
        load_tool_specs(str(p))


def test_post_with_body_is_allowed(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: POST, path: /a}
            inputs:
              - {name: payload, in: body, required: true}
        """,
    )
    spec = load_tool_specs(str(p))[0]
    assert spec.inputs[0].location == "body"


def test_invalid_input_type_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            inputs:
              - {name: n, type: integer, in: query}
        """,
    )
    with pytest.raises(AppError, match="定义非法"):
        load_tool_specs(str(p))


def test_invalid_tool_name_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: query-logs-x
            http: {method: GET, path: /a}
        """,
    )
    with pytest.raises(AppError, match="不合法"):
        load_tool_specs(str(p))


def test_tool_name_python_keyword_rejected(env, tmp_path):
    """tool 名若是 Python 保留字，exec 生成会 SyntaxError，loader 必须 fail-closed。"""
    p = _write(
        tmp_path,
        """\
        tools:
          - name: lambda
            http: {method: GET, path: /a}
        """,
    )
    with pytest.raises(AppError, match="保留字"):
        load_tool_specs(str(p))


def test_input_name_non_identifier_rejected(env, tmp_path):
    """入参名含连字符等非法字符：须在 loader 拒绝而非 exec 阶段 SyntaxError。"""
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            inputs:
              - {name: service-name, in: query}
        """,
    )
    with pytest.raises(AppError, match="不合法"):
        load_tool_specs(str(p))


def test_input_name_keyword_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            inputs:
              - {name: in, in: query}
        """,
    )
    with pytest.raises(AppError, match="保留字"):
        load_tool_specs(str(p))


def test_input_name_leading_underscore_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a}
            inputs:
              - {name: _x, in: query}
        """,
    )
    with pytest.raises(AppError, match="下划线"):
        load_tool_specs(str(p))


def test_http_path_with_query_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http:
              method: GET
              path: "/a?from=1"
        """,
    )
    with pytest.raises(AppError, match="不应包含"):
        load_tool_specs(str(p))


def test_http_base_url_bad_scheme_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /a, base_url: ftp://logs.example}
        """,
    )
    with pytest.raises(AppError, match="http:// 或 https://"):
        load_tool_specs(str(p))


def test_path_param_valid(env, tmp_path):
    """http.path 占位符 {requestId} + in:path required 入参 -> 合法。"""
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: "/api/chain/{requestId}"}
            inputs:
              - {name: requestId, in: path, required: true}
              - {name: logLevel, in: query}
        """,
    )
    spec = load_tool_specs(str(p))[0]
    assert spec.inputs[0].location == "path"
    assert spec.http.path == "/api/chain/{requestId}"


def test_path_param_must_be_required(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: "/api/chain/{requestId}"}
            inputs:
              - {name: requestId, in: path}
        """,
    )
    with pytest.raises(AppError, match="required"):
        load_tool_specs(str(p))


def test_path_placeholder_missing_input_rejected(env, tmp_path):
    """路径有占位符但没声明对应 in:path 入参 -> 拒绝（运行时必然 KeyError）。"""
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: "/api/chain/{requestId}"}
            inputs:
              - {name: startTime, in: query}
        """,
    )
    with pytest.raises(AppError, match="缺少对应的 in:path"):
        load_tool_specs(str(p))


def test_path_input_without_placeholder_rejected(env, tmp_path):
    p = _write(
        tmp_path,
        """\
        tools:
          - name: q
            http: {method: GET, path: /api/chain/x}
            inputs:
              - {name: requestId, in: path, required: true}
        """,
    )
    with pytest.raises(AppError, match="不在 http.path 占位符"):
        load_tool_specs(str(p))
