"""按 ToolSpec 生成一个带显式签名的 MCP 工具函数（schema-driven factory）。

每个 spec 的 inputs 决定函数入参（Annotated[type, Field(...)]，FastMCP 据此生成 JSON
Schema 并真实校验）；调用时按 location（query/body/path）拆分后交给 UpstreamClient 执行：
path 入参替换进 http.path 模板（URL 转义），query 入参放 URL query，body 入参放 JSON body。
函数名取自 spec.name（loader 已保证是合法 Python 标识符），用 exec 按同一模板生成。
"""
import re
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from applog_mcp_server.config import get_settings
from applog_mcp_server.errors import AppError, ErrorCode
from applog_mcp_server.http.upstream import UpstreamClient
from applog_mcp_server.tools.loader import ToolSpec


def build_tool(spec: ToolSpec, *, transport: Any = None) -> Any:
    """构建工具函数。transport 注入用于测试（httpx.MockTransport）。"""
    client = UpstreamClient(transport=transport)
    base_url = spec.http.base_url or get_settings().applog_default_base_url
    if not re.match(r"^https?://", base_url):
        # spec.http.base_url 已在 loader 校验；这里兜底校验 env 回落值，配置漂移在启动期暴露
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"tool {spec.name!r} 上游 base_url 非 http(s)://，"
            f"请检查 tools.yaml 或 APPLOG_DEFAULT_BASE_URL：{base_url!r}",
        )
    method = spec.http.method
    path_template = spec.http.path

    query_names = [i.name for i in spec.inputs if i.location == "query"]
    body_names = [i.name for i in spec.inputs if i.location == "body"]
    path_names = [i.name for i in spec.inputs if i.location == "path"]

    def _run(params: dict[str, str]) -> dict[str, Any]:
        query = {k: params[k] for k in query_names if params.get(k) is not None}
        body = {k: params[k] for k in body_names if params.get(k) is not None}
        # path 入参替换进模板（loader 已保证占位符齐备、必填）；逐段 URL 转义
        resolved_path = path_template
        for name in path_names:
            resolved_path = resolved_path.replace(f"{{{name}}}", quote(params[name], safe=""))
        return client.execute(
            tool_name=spec.name,
            method=method,
            base_url=base_url,
            path=resolved_path,
            params=query or None,
            body=body or None,
        )

    # 依据 spec.inputs 生成形如的内层签名：
    #   def query_app_logs(*, startTime: Annotated[str, Field(...)], endTime: ...,
    #                      logLevel: Annotated[str | None, Field(...)] = None) -> dict:
    signature_lines: list[str] = []
    for inp in spec.inputs:
        desc = repr(inp.description)
        if inp.required:
            signature_lines.append(f"{inp.name}: Annotated[str, Field(description={desc})]")
        else:
            signature_lines.append(
                f"{inp.name}: Annotated[str | None, Field(description={desc})] = None"
            )

    if signature_lines:
        joined = ",\n        ".join(signature_lines)
        source = (
            f"def {spec.name}(*,\n"
            f"    {joined},\n"
            "):\n"
            "    _p = {k: v for k, v in locals().items() if v is not None}\n"
            "    return _run(_p)\n"
        )
    else:
        source = f"def {spec.name}():\n    return _run({{}})\n"

    namespace: dict[str, Any] = {
        "Annotated": Annotated,
        "Field": Field,
        "_run": _run,
    }
    try:
        exec(source, namespace)  # noqa: S102 - 模板受 loader 标识符/类型白名单约束
    except Exception as exc:  # noqa: BLE001 - 兜底：任何生成失败都归为配置错误而非裸崩溃
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"tool {spec.name!r} 函数生成失败（检查 tools.yaml 该段入参声明）：{exc}",
        ) from exc
    return namespace[spec.name]
