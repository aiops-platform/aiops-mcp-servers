# applog-mcp-server

声明式把**多个 HTTP 日志查询接口**自动注册成 MCP 只读工具的 Server（Streamable HTTP）。每个接口在 `config/tools.yaml` 里声明一段，启动后即为一个可供 agent 调用的 MCP tool。

## 功能特性

- **配置即工具**：`config/tools.yaml` 声明「HTTP 接口 → MCP tool」，新增查询只改 YAML、不写代码；
- 入参由声明生成（FastMCP 真实校验），工具统一标注 `readOnlyHint`（agent 侧可 ALLOW）；
- 支持 GET(query 参数) / POST(query + JSON body)；返回**通用透传**（不假设字段）；
- 上游调用：显式超时 + **流式读取按字节上限截流**（内存有界，码点边界安全截断）、跟随 3xx；
  非 2xx / 超时归一为结构化失败，失败/认证拒绝记服务端 warning 日志（含 method/url/耗时）；
- fail-closed 配置校验：tools.yaml 非法 / 工具名与入参名非合法标识符或为 Python 保留字 /
  名字重复 / `in:body` 配 GET / path 含 query / base_url 非 http(s) → 拒绝启动并报中文错误；
- 可选 MCP Bearer 认证（`AUTH_TOKEN` 为空则不启用）；`ENVIRONMENT=production` 强制要求 token；
  `/health` 存活探针。

## 目录结构

```
servers/applog-mcp-server/
├── .env.example                # 通用运行配置模板
├── config/tools.yaml           # ★ 工具声明（一个 HTTP 接口 = 一个 tool）
├── pyproject.toml
└── src/applog_mcp_server/
    ├── server.py               # FastMCP 组装 + /health + 可选认证
    ├── config.py               # pydantic-settings（.env / 环境变量）
    ├── errors.py               # ErrorCode + AppError
    ├── auth/token_verifier.py  # StaticTokenVerifier（可选认证用）
    ├── http/upstream.py        # 上游调用封装（超时/截断/归一）
    └── tools/
        ├── loader.py           # tools.yaml → ToolSpec（fail-closed 校验）
        ├── factory.py          # spec → 带显式签名的工具函数
        └── __init__.py         # 读 tools.yaml 逐个注册
```

## 快速开始

```bash
cd servers/applog-mcp-server
cp .env.example .env
uv run python -m applog_mcp_server        # 默认 http://127.0.0.1:8200
```

启动日志会打印已注册工具：`registered 1 tools: query_app_logs`。

## 添加一个 HTTP 查询接口（核心用法）

在 `config/tools.yaml` 的 `tools:` 下追加一段：

```yaml
  - name: query_by_trace          # MCP 工具名（agent 侧可见）
    description: 按链路 ID 查询应用日志
    http:
      method: GET
      path: /api/sip-aiops/trace    # base_url 缺省回落 env APPLOG_DEFAULT_BASE_URL
    inputs:
      - {name: traceId, type: string, in: query, required: true, description: 链路 ID}
      - {name: serviceName, type: string, in: query, description: 服务名}
    response: {mode: passthrough}
```

重启 server，`tools/list` 即出现 `query_by_trace`。入参位置由 `inputs.in` 决定：
- `in: query`（默认）→ 放 URL query string；
- `in: body` → 放进 JSON body，仅 `method: POST` 允许；
- `in: path` → 替换进 `http.path` 的占位符 `{name}`（须 `required: true`），值做 URL 转义。例如「按 requestId 查链路日志」：

```yaml
  - name: query_chain_log_by_request_id
    description: 按 requestId 查询整条链路日志
    http:
      method: GET
      path: /api/sip-aiops/app-log/chain/{requestId}
    inputs:
      - {name: requestId, in: path, required: true, description: 链路 ID}
      - {name: logLevel, in: query, description: 日志级别}
```

## 配置项（.env / 环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` 下强制要求 `AUTH_TOKEN`，否则拒绝启动 |
| `BIND_HOST` / `BIND_PORT` | `127.0.0.1` / `8200` | 监听地址 |
| `AUTH_TOKEN` | (空) | 静态 Bearer Token；空则不启用 MCP 认证 |
| `APPLOG_DEFAULT_BASE_URL` | `http://localhost:8080` | tools.yaml 里 `http.base_url` 缺省时回落 |
| `APPLOG_TOOLS_FILE` | `config/tools.yaml` | 工具定义文件路径 |
| `APPLOG_REQUEST_TIMEOUT_SEC` | `30.0` | 单次上游请求超时（秒） |
| `APPLOG_MAX_RESPONSE_BYTES` | `1048576` | 单次工具返回字节上限（超出截断并标注） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 客户端集成

```bash
# Claude Code
claude mcp add applog --transport http http://127.0.0.1:8200/mcp
# 若启用了认证：追加 --header "Authorization: Bearer <token>"
```

或通用 MCP JSON 配置：

```json
{
  "mcpServers": {
    "applog": {
      "type": "http",
      "url": "http://127.0.0.1:8200/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## 工具返回（v1 通用透传）

```json
{ "success": true,  "upstream_status": 200, "data": <上游 JSON>, "total": <数组长度|null>, "truncated": false, "took_ms": 12 }
{ "success": false, "error": "[TOOL_EXECUTION_ERROR] upstream HTTP 500: ...", "upstream_status": 500 }
```

## 测试

```bash
cd /path/to/aiops-mcp-servers
uv run pytest servers/applog-mcp-server/tests     # 或根目录 uv run pytest 跑全部
uv run ruff check servers/applog-mcp-server
uv run mypy servers/applog-mcp-server/src/applog_mcp_server
```
