"""tools.yaml 声明式工具定义解析（fail-closed）。

结构：tools 列表，每个元素一个 HTTP 日志查询接口 = 一个 MCP tool。
校验失败（YAML 非法 / 未知字段 / 名字重复 / body 配 GET 等）直接抛 AppError(CONFIG_ERROR)。
"""
from __future__ import annotations

import keyword
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from applog_mcp_server.config import get_settings
from applog_mcp_server.errors import AppError, ErrorCode

_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}$")

# http.path 里的动态路径占位符：{requestId} -> 对应一个 in:path 入参
_PATH_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")

_METHODS = ("GET", "POST")


def _check_ident(name: str, kind: str) -> None:
    """校验名字能安全用作 Python 函数/参数名（exec 模板的前提）。"""
    if not _IDENT_RE.match(name):
        raise ValueError(
            f"{kind} {name!r} 不合法：须以字母开头，仅含字母/数字/下划线（最长 64）"
        )
    if name.startswith("_") or keyword.iskeyword(name):
        raise ValueError(
            f"{kind} {name!r} 不合法：不能以下划线开头或为 Python 保留字"
        )


class HttpSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = "GET"
    path: str
    base_url: str | None = None  # 缺省回落到 env APPLOG_DEFAULT_BASE_URL

    @model_validator(mode="after")
    def _normalize(self) -> HttpSpec:
        self.method = self.method.upper()
        if self.method not in _METHODS:
            raise ValueError(f"http.method 仅支持 {'/'.join(_METHODS)}，收到 {self.method!r}")
        if not self.path.startswith("/"):
            raise ValueError(f"http.path 必须以 / 开头，收到 {self.path!r}")
        if "?" in self.path or "#" in self.path:
            raise ValueError("http.path 不应包含 query 或 fragment（参数请用 inputs 声明）")
        if self.base_url is not None:
            if not self.base_url.strip():
                raise ValueError("http.base_url 显式给出时不能为空串（留空则回落默认）")
            if not re.match(r"^https?://", self.base_url):
                raise ValueError("http.base_url 必须以 http:// 或 https:// 开头")
        return self


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    name: str
    type: Literal["string"] = "string"  # v1 仅支持 string（HTTP 参数本就是字符串）
    location: Literal["query", "body", "path"] = Field(default="query", validation_alias="in")
    required: bool = False
    description: str = ""


class ResponseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["passthrough"] = "passthrough"  # v1 通用透传


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    http: HttpSpec
    inputs: list[InputSpec] = []
    response: ResponseSpec = ResponseSpec()

    @model_validator(mode="after")
    def _validate(self) -> ToolSpec:
        _check_ident(self.name, "tool name")

        placeholders = set(_PATH_PLACEHOLDER_RE.findall(self.http.path))
        seen: set[str] = set()
        path_inputs: set[str] = set()
        for inp in self.inputs:
            _check_ident(inp.name, f"tool {self.name!r} 的入参名")
            if inp.name in seen:
                raise ValueError(f"tool {self.name!r} 的入参名重复：{inp.name}")
            seen.add(inp.name)
            if inp.location == "body" and self.http.method != "POST":
                raise ValueError(
                    f"tool {self.name!r} 入参 {inp.name!r}: in=body 仅支持 method=POST"
                )
            if inp.location == "path":
                if inp.name not in placeholders:
                    raise ValueError(
                        f"tool {self.name!r} 入参 {inp.name!r}: "
                        "in=path 名字不在 http.path 占位符中"
                    )
                if not inp.required:
                    raise ValueError(
                        f"tool {self.name!r} 入参 {inp.name!r}: in=path 必须 required=true"
                    )
                path_inputs.add(inp.name)

        uncovered = placeholders - path_inputs
        if uncovered:
            names = ", ".join(sorted(uncovered))
            raise ValueError(
                f"tool {self.name!r}: http.path 占位符 {{{names}}} 缺少对应的 in:path 必填入参"
            )
        return self


def load_tool_specs(tools_file: str | None = None) -> list[ToolSpec]:
    """读取并校验 tools.yaml，返回 ToolSpec 列表。失败即抛 CONFIG_ERROR。"""
    path = Path(tools_file or get_settings().applog_tools_file)
    if not path.exists():
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"工具定义文件不存在：{path}。请在 APPLOG_TOOLS_FILE 指定或补建 config/tools.yaml",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AppError(
            ErrorCode.CONFIG_ERROR, f"tools.yaml 读取失败（{path}）：{exc}"
        ) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AppError(ErrorCode.CONFIG_ERROR, f"tools.yaml 解析失败：{exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list):
        raise AppError(ErrorCode.CONFIG_ERROR, "tools.yaml 顶层须为 { tools: [ ... ] } 结构")

    specs: list[ToolSpec] = []
    names: set[str] = set()
    for i, entry in enumerate(raw["tools"]):
        if not isinstance(entry, dict):
            raise AppError(ErrorCode.CONFIG_ERROR, f"tools[{i}] 须为映射（name/http/inputs...）")
        try:
            spec = ToolSpec.model_validate(entry)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.CONFIG_ERROR, f"tools[{i}] 定义非法：{_first_error(exc)}"
            ) from exc
        if spec.name in names:
            raise AppError(
                ErrorCode.CONFIG_ERROR, f"tool 名字重复：{spec.name}（须唯一）"
            )
        names.add(spec.name)
        specs.append(spec)

    if not specs:
        raise AppError(ErrorCode.CONFIG_ERROR, "tools.yaml 未声明任何 tool（tools 列表为空）")
    return specs


def _first_error(exc: ValidationError) -> str:
    """把 pydantic 校验错误压缩成一行可读信息。"""
    err = exc.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg', '')}" if loc else str(err.get("msg", ""))
