# Git MCP Server 设计文档

> 状态：**待评审**（v0.3，SDK 路线调整：锁定官方 v1.x，保留评审全部采纳项）
> 作者：AIOps MCP 团队
> 日期：2026-09-02
> 参考项目：[sip-mcp-gateway](../../../acc-aiops-platform/sip-mcp-gateway)（SIP 统一 MCP 网关）

---

## 1. 背景与目标

### 1.1 背景

`aiops-mcp-servers` 仓库将承载多个独立的 MCP（Model Context Protocol）Server，供平台内其他服务 / Agent 编排器配置并调用。第一个要落地的是 **Git MCP Server**，提供对本地 Git 仓库的只读查询能力（状态、日志、差异、内容、行级注释、分支）。

参考项目 `sip-mcp-gateway` 已实现 6 个 Git 只读工具（FastMCP + SSE transport），但其存在需要修复的缺陷：

1. **认证未接线**：`security/auth.py` 定义了 Bearer Token 校验函数，但 `server.py` 从未调用，认证形同虚设。
2. **Pydantic 校验是死代码**：定义了 `GitStatusInput` 等输入模型，但工具函数直接接收普通参数，`field_validator` 永远不会执行。
3. **无超时控制**：GitPython 的 `repo.git.*` 调用没有超时，大仓库 / 异常仓库可能挂死进程。
4. **SSE transport 已过时**：MCP 规范已于 2026-04-01 弃用 SSE，Streamable HTTP 是当前标准。

### 1.2 目标

- 提供一个**独立可部署**、**安全**、**可测试**、**可观测**的 Git 只读 MCP Server。
- 采用**业界最佳实践**：
  - 官方 `mcp` Python SDK **v1.x（FastMCP）** + **原生认证**（`token_verifier` + `AuthSettings`），不自建认证中间件；
  - **Streamable HTTP**（`streamable_http_app()`），无状态服务设计；
  - **Origin / Host 头校验**（防 DNS rebinding，MCP 规范强制要求）；
  - subprocess 命令执行（防注入 + 超时）；完整单元 / 安全 / 端到端测试。
- 采用 **uv workspace 单仓** 组织：每个 Server 是独立可安装、可部署的子包，后续新增 MCP Server 只需在 `servers/` 下加子目录。
- 提供清晰的**客户端集成方式**（CLI / Python 客户端 / 通用 MCP 配置 / Docker）。

### 1.3 SDK 版本决策（v0.3 调整）

- 上一版（v0.2）因评审建议「用 SDK 原生认证」而升级到官方 v2（MCPServer）。经核实与团队讨论，**改为锁定官方 v1.x**：
  - **v1 后期版本（≥1.13，官方 1.27 文档确认）同样具备原生认证**（`token_verifier` + `auth=AuthSettings(...)` + `auth_server_provider=`），评审的核心诉求不受影响；
  - v2 的 `add_middleware` 标注 provisional、import 路径存在漂移风险；本项目**优先稳定性**；
  - v1 与本仓库参考项目（sip-mcp-gateway）同族，模式可复用。
- 缺失的 v2 特性对**本架构无实质影响**：限流走 HTTP 层（覆盖更全面）、无状态由设计保证（不依赖服务端会话）、2026-07-28 协议不宣告（现代客户端照常兼容）。
- **v2 列为后续升级项**：届时用 uv.lockfile 锁定精确版本后再迁移。

### 1.4 非目标（本期不做）

- 不做 Git **写操作**（commit / push / merge / reset / 远程删除等）。
- 不内置**授权服务器（IdP）**：本服务是 OAuth 2.1 的 **resource server**，只校验 `Authorization: Bearer` 头，不签发 token；签发由外部 IdP（Auth0 / Keycloak / Entra 等）承担。
- 不做仓库内容的**同步/更新**（依赖外部 initContainer + sidecar cron `git fetch`，与参考项目一致）。
- 不做 Jira / Confluence 等其他领域工具（本期仅 Git）。

---

## 2. 范围与决策

| 决策项 | 选择 | 理由 |
|---|---|---|
| 技术栈 | Python + 官方 `mcp` SDK **v1.x（`mcp.server.fastmcp.FastMCP`）** | 与参考项目同族；v1 后期版本具备原生认证，稳定性优先 |
| SDK 版本 | `mcp>=1.13,<2.0`（Phase 0 实测锁定精确版本） | 覆盖原生认证能力，避开 v2 provisional API |
| 工具范围 | **纯只读**（6 个工具） | 作为共享基础设施风险最低 |
| 传输协议 | **Streamable HTTP**（`streamable_http_app()` + `json_response=True`） | 规范已弃用 SSE；服务间 / Claude Code / 主流客户端均支持 |
| 协议版本 | 不宣告 2026-07-28 stateless（SEP-2577），但实现**不依赖服务端会话** | 现代客户端（含 2026-07-28）对旧行为完全兼容 |
| 认证 | **SDK 原生**：`FastMCP(token_verifier=..., auth=AuthSettings(...))`；默认 `StaticTokenVerifier`，生产可切 OAuth 2.1（`auth_server_provider` / introspection） | 采纳评审意见，用原生能力替代自建认证中间件 |
| Origin/Host 校验 | **HTTP 层强制校验**（薄中间件或反向代理），无效来源 → 403 | MCP 规范 MUST；官方 SDK 默认不开启 DNS rebinding 防护 |
| 限流 | **HTTP 层 Token Bucket**（v1 无 add_middleware；HTTP 层覆盖 /health、/metrics 及传输层，更全面） | 与参考项目同逻辑，方案稳定 |
| 目录结构 | **uv workspace 单仓**（virtual root + `servers/*` 子包） | 独立部署 + 共享工具链 |
| Git 执行方式 | **subprocess + list 参数**（不用 GitPython） | 无 shell 注入、显式超时、输出上限、依赖更少 |
| 可观测性 | `GET /metrics`（Prometheus 格式）+ 结构化日志 + OpenTelemetry（可选） | 采纳评审意见 |
| 配置 | `pydantic-settings`（.env / 环境变量） | 与参考项目一致 |

---

## 3. 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│  调用方服务 / MCP Client（sip-agent-orchestrator、其他服务...）     │
│    mcp.client.streamable_http.streamablehttp_client               │
└───────────────────────────┬──────────────────────────────────────┘
                            │  Streamable HTTP :8000/mcp
                            │  Authorization: Bearer <token>
                            │  (无状态，每请求自包含)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  git-mcp-server（独立进程，uvicorn 启动）                           │
│                                                                  │
│  Starlette App                                                   │
│  ├─ GET  /health   存活探针（免认证）                              │
│  ├─ GET  /metrics  Prometheus 指标（免认证）                       │
│  └─ Mount /  FastMCP 应用（streamable_http_app()，/mcp）          │
│                                                                  │
│  HTTP 层中间件（顺序执行）                                         │
│  ① SecurityMiddleware   Origin 校验→403 / Host 校验→403 / trace_id │
│  ② RateLimitMiddleware   Token Bucket 限流（→429）                 │
│                                                                  │
│  FastMCP（官方 SDK v1.x，原生能力）                                 │
│  ├─ token_verifier = StaticTokenVerifier（/ OAuth 接入点）          │
│  ├─ auth = AuthSettings(...)      （resource server 元数据）        │
│  └─ 6 个只读工具                                                  │
│    git_status │ git_log │ git_diff │ git_show │ git_blame │       │
│    git_branch                                                    │
│            │                                                     │
│            ▼                                                     │
│   GitExecutor（subprocess + list 参数 + timeout + 安全 env）        │
│            │                                                     │
│            ▼                                                     │
│   本地 Git 仓库（GIT_ALLOWED_ROOTS 白名单内，只读）                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. 目录结构

```
aiops-mcp-servers/                       # uv workspace 根（virtual project）
├── pyproject.toml                       # [project] name + [tool.uv.workspace] + [dependency-groups] dev
├── uv.lock                              # uv lock 生成（提交入版本库）
├── README.md                            # workspace 总览 + 如何新增 server
├── .gitignore
└── servers/
    └── git-mcp-server/                  # 独立可部署子包（本设计的主体）
        ├── pyproject.toml               # 包名 git-mcp-server，src layout，运行时依赖
        ├── README.md                    # Server 文档：架构 / 配置 / 客户端集成 / 安全
        ├── .env.example                 # 全部配置项示例
        ├── Dockerfile                   # 多阶段构建，python:3.12-slim，HEALTHCHECK /health
        ├── src/git_mcp_server/
        │   ├── __init__.py              # __version__
        │   ├── __main__.py              # python -m git_mcp_server
        │   ├── server.py                # create_server() 工厂 + build_app() + main()
        │   ├── config.py                # pydantic-settings Settings + get_settings()（lru_cache）
        │   ├── errors.py                # ErrorCode 枚举 + AppException
        │   ├── auth/
        │   │   ├── __init__.py
        │   │   ├── token_verifier.py    # TokenVerifier 实现：StaticTokenVerifier（+ OAuth 接入点）
        │   │   └── sandbox.py           # validate_repo_path() 路径沙箱（fail-closed）
        │   ├── middleware/
        │   │   ├── __init__.py
        │   │   ├── security.py          # Origin/Host 校验（→403）+ 请求日志 + trace_id
        │   │   └── rate_limit.py        # TokenBucket + RateLimitMiddleware
        │   ├── metrics.py               # Prometheus 指标：请求数/延迟/错误/限流/git 命令数
        │   └── tools/
        │       ├── __init__.py          # _ALL_TOOLS 注册表 + register_all_tools()
        │       ├── base.py              # tool_guard 装饰器（统一错误 → {success, error, ...}）
        │       └── git/
        │           ├── __init__.py
        │           ├── executor.py      # GitExecutor：subprocess 封装（超时/安全 env/输出上限）
        │           └── operations.py    # 6 个只读工具（Annotated[..., Field(...)] 参数校验）
        └── tests/
            ├── conftest.py              # git_repo / git_repo_multi fixtures（subprocess 建临时仓库）
            ├── test_config.py
            ├── test_auth.py             # TokenVerifier 单元测试
            ├── test_sandbox.py          # 路径沙箱边界（含安全专项）
            ├── test_security.py         # Origin/Host 403、ENVIRONMENT=production 强制认证
            ├── test_middleware.py       # 限流 429、trace_id 日志
            ├── test_metrics.py          # /metrics 格式与计数器
            ├── test_git_operations.py   # 6 个工具
            ├── test_server.py           # 端到端：/health + /metrics + 401/403 + tools/list
            └── test_protocol_compat.py  # 官方 ClientSession + streamablehttp_client 协议兼容测试
```

---

## 5. 关键设计决策

### 5.1 认证：SDK v1 原生能力（评审核心诉求，v1 同样具备）

官方 `mcp` SDK v1 后期版本（≥1.13）的 FastMCP 原生支持 resource-server 模式认证：**不签发 token，只校验每个请求的 `Authorization: Bearer <token>`**。API 为构造参数（官方 1.27 文档确认）：

```python
# server.py（简化示意，精确 API 以 Phase 0 实测为准）
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import AccessToken, TokenVerifier

class StaticTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        self._token = token
    async def verify_token(self, token: str):
        return AccessToken(token=token, client_id="git-mcp-server", scopes=["git:read"]) \
            if token == self._token else None

server = FastMCP(
    "git-mcp-server",
    json_response=True,
    token_verifier=StaticTokenVerifier(settings.auth_token),
    auth=AuthSettings(
        issuer_url=...,                                  # OAuth 模式时填写
        resource_server_url=f"http://{host}:{port}/mcp",
    ),
)
```

要点：
- `TokenVerifier` 是协议（async `verify_token(token) -> AccessToken | None`），SDK 不关心 token 怎么验。`StaticTokenVerifier` 支持静态 token；生产可换 RFC 7662 introspection 或 JWT 校验，**只换 verifier，其他代码不动**。
- 认证仅作用于 HTTP transport；stdio 默认不认证（本服务不用 stdio）。
- **v0.2 曾计划用 v2 MCPServer；经稳定性权衡改为 v1，原生认证能力不变**。

### 5.2 Origin / Host 头校验：MCP 规范强制项（采纳评审意见，高优先级）

- **规范要求**（Streamable HTTP）：服务端 **MUST** 校验 `Origin` 头，若存在且不在允许列表 → **HTTP 403**（防 DNS rebinding）。
- 官方 Python SDK **默认不开启** DNS rebinding 防护（SDK advisory 确认），故需自行处理。
- 设计：HTTP 层 `SecurityMiddleware` 校验
  - `Origin` 存在且不在 `ALLOWED_ORIGINS` → **403**；`Origin` 缺失（非浏览器客户端如 CLI/服务间调用）→ 放行。
  - `Host` 校验（防 rebinding 纵深防御）：不在 `ALLOWED_HOSTS`（默认回环地址）→ **403**。
- 预期行为说明：服务间调用与官方 CLI/SDK 通常**不带 Origin 头**，默认放行；若遇到非标准客户端自动带 `Origin: file://` 等，指导将预期 Origin 加入白名单。
- 部署提示：生产若经 nginx/网关，也可在此层校验；两种方式择一，避免双重维护。

### 5.3 中间件与限流（HTTP 层为主）

- **认证**走 SDK 原生，**不**自建认证中间件（采纳评审）。
- **限流**：v1 无 `add_middleware`，采用 **HTTP 层 `RateLimitMiddleware`**（Token Bucket，与参考项目同逻辑）。相比 v2 的 MCP 消息级 middleware，HTTP 层限流覆盖 `/health`、`/metrics` 及传输层，更全面。
- **日志 / trace**：HTTP 层 `SecurityMiddleware` 内附 trace_id 请求日志。

### 5.4 协议版本与无状态（2026-07-28）

- MCP 2026-07-28 修订引入 **stateless core（SEP-2577）**，为**可选启用**。v1 不宣告该协议版本，现代客户端（含 2026-07-28）对旧行为完全兼容。
- 本服务**按无状态设计**：工具为纯查询、不缓存、不依赖服务端会话状态，每个请求自包含；未来升级 v2 时切换 `stateless_http=True` 成本极低。

### 5.5 Git 执行：subprocess + list 参数（替代 GitPython）

| 对比项 | GitPython（参考项目） | subprocess（本设计） |
|---|---|---|
| Shell 注入 | `repo.git.*` 走 subprocess list，本身安全 | 显式 list 参数，无 shell，安全 |
| 超时 | 无 | `timeout=`（默认 30s） |
| 环境控制 | 需手动 update_environment | 统一注入 `GIT_TERMINAL_PROMPT=0`、`GIT_PAGER=cat`、`LC_ALL=C` |
| 输出上限 | 仅靠后置截断 | 执行期即可限制 |
| 依赖 | gitpython | 无（更少依赖） |

输出截断在**字节边界**处理：先按字节截断，再按 UTF-8 解码失败时回退到上一个合法边界，避免乱码；截断时在内容末尾追加 `... (truncated at <N> bytes, please narrow the scope)` 提示（评审建议，提升 LLM 对结果不完整的感知）。

### 5.6 参数校验真正生效（`Annotated[type, Field(...)]`）

用 `Annotated` 元数据标注工具函数参数，FastMCP 据此生成客户端可见 JSON Schema 并**真实执行校验**：

```python
async def git_log(
    repo_path: Annotated[str, Field(min_length=1, description="Git 仓库绝对路径")],
    max_count: Annotated[int, Field(20, ge=1, le=100, description="最大返回提交数")],
    branch: Annotated[str | None, Field(None, max_length=200, description="分支名")] = None,
) -> dict:
    """显示 Git 仓库的提交日志。"""
    ...
```

校验规则：`repo_path` 必填绝对路径；`max_count` 1–100、`max_lines` 1–5000；`file_path` 可选相对路径；rev 参数（target/commit）长度上限且**禁止以 `-` 开头**（防参数注入）。

### 5.7 可观测性（采纳评审意见）

- `GET /metrics`：Prometheus 文本格式，指标：
  - `mcp_requests_total`、`mcp_request_duration_seconds`（直方图）、`mcp_errors_total`
  - `mcp_rate_limited_total`、`git_commands_total`
- **OpenTelemetry**（可选，v1 无内建中间件，自行接入）：`opentelemetry-instrumentation` 打点，`OTEL_EXPORTER_OTLP_ENDPOINT` 导出 trace。
- 结构化日志：JSON 行格式，含 `trace_id`、`tool`、`duration_ms`。

### 5.8 生产环境强制认证

`ENVIRONMENT` 配置项（`development` | `production`）：
- `production` 下若 `AUTH_TOKEN` 为空（未配置认证）→ **拒绝启动**，避免配置疏忽导致服务裸奔。
- `production` 下 `GIT_ALLOWED_ROOTS` 为空 → 拒绝启动（启动期即报错，更早暴露问题）。

### 5.9 工具可开关（可选加分项）

提供 `TOOLS_ENABLED` 配置（逗号分隔工具名，为空则全部启用），支持运行时禁用重工具（如 `git_blame`），为未来弹性预留。

---

## 6. 工具规格

所有工具返回**统一结构**：

```json
{ "success": true,  ... 业务字段 ... }
{ "success": false, "error": "[ErrorCode] message" }
```

| 工具 | 参数 | 底层命令 | 返回关键字段 | 资源限制 |
|---|---|---|---|---|
| `git_status` | `repo_path` | `git status --porcelain=v1 --branch` | branch、changed_files、untracked_files、is_clean | 输出 ≤ 1MB |
| `git_log` | `repo_path`, `max_count`(1–100), `branch?` | `git log --no-pager -n N [branch] --pretty=format:%H%x00%an%x00%ae%x00%aI%x00%s`（NUL 分隔解析） | commits[{hash,author,email,date,message}]、total | max_count ≤ 100 |
| `git_diff` | `repo_path`, `target`(默认 HEAD), `file_path?` | `git diff [target] [-- file]` | diff（unified 文本）、truncated | 输出 ≤ 1MB，超限截断+标注 |
| `git_show` | `repo_path`, `target` | `git show target` | content、truncated | 输出 ≤ 1MB |
| `git_blame` | `repo_path`, `file_path`, `commit?`, `max_lines`(1–5000) | `git blame --line-porcelain [rev] -- file` | lines[{hash,author,date,summary,content}]、total、truncated | max_lines ≤ 5000 |
| `git_branch` | `repo_path` | `git branch --format=%(refname:short)` + `git rev-parse --abbrev-ref HEAD` | branches、current | 输出 ≤ 1MB |

**解析鲁棒性**：log/blame 用 NUL / porcelain 格式解析，对特殊字符免疫；detached HEAD 时 current 返回 `HEAD (detached)`；非 Git 仓库 / 路径不存在返回结构化错误（`INVALID_REQUEST`）。

---

## 7. 安全设计（多层防护）

| 层 | 机制 | 说明 |
|---|---|---|
| 1 | **网络隔离** | 默认绑定 `127.0.0.1:8000`，deny-by-default |
| 2 | **原生认证** | FastMCP `token_verifier` + `AuthSettings`；默认 `StaticTokenVerifier`，生产可切 OAuth 2.1 / token introspection |
| 3 | **Origin / Host 校验** | MCP 规范 MUST；无效 Origin → 403；Host 默认回环白名单 → 403（防 DNS rebinding） |
| 4 | **生产强制认证** | `ENVIRONMENT=production` 下无认证 / 无白名单配置 → 拒绝启动 |
| 5 | **路径沙箱** | `realpath()` + 白名单前缀匹配（`GIT_ALLOWED_ROOTS`）+ 禁止 `..` + **白名单未配置则拒绝所有（fail-closed）** |
| 6 | **只读 + 资源限制** | 仅只读子命令；subprocess `timeout`（默认 30s）；输出字节上限（1MB）；rev 参数禁止以 `-` 开头 |
| 7 | **输入校验 + 限流** | `Annotated Field` 范围/长度校验；HTTP 层 Token Bucket 限流（可选） |

路径沙箱核心逻辑（`sandbox.py`，与参考项目一致并保留）：

```python
def validate_repo_path(repo_path: str) -> str:
    if ".." in repo_path:                          # 纵深防御：禁止路径穿越
        raise AppException(ErrorCode.PATH_NOT_ALLOWED, "Path traversal detected: '..' is forbidden")
    real = os.path.realpath(repo_path)             # 解析符号链接
    allowed = get_settings().git_allowed_roots.strip()
    if not allowed:                                 # fail-closed
        raise AppException(ErrorCode.PERMISSION_DENIED, "No allowed roots configured. Set GIT_ALLOWED_ROOTS.")
    for root in allowed.split(","):
        root = os.path.realpath(root.strip())
        if real == root or real.startswith(root + os.sep):
            return real
    raise AppException(ErrorCode.PERMISSION_DENIED, f"Path '{repo_path}' is not in allowed roots")
```

---

## 8. 配置项（.env / 环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` \| `production`；production 下强制认证（见 5.8） |
| `BIND_HOST` | `127.0.0.1` | 绑定地址 |
| `BIND_PORT` | `8000` | 监听端口 |
| `AUTH_TOKEN` | (空) | 静态 Bearer Token（`StaticTokenVerifier` 用）；production 下为空则拒绝启动 |
| `GIT_ALLOWED_ROOTS` | (空) | Git 仓库根目录白名单（逗号分隔）；为空则**拒绝所有仓库** |
| `GIT_COMMAND_TIMEOUT_SEC` | `30` | 每条 git 命令超时（秒） |
| `GIT_MAX_OUTPUT_BYTES` | `1048576` | 单次工具输出上限（字节，默认 1MB） |
| `ALLOWED_ORIGINS` | (空) | Origin 白名单（逗号分隔）；为空则仅放行无 Origin 的请求（非浏览器客户端） |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,::1` | Host 白名单（逗号分隔）；为空则仅放行无 Host 的请求 |
| `RATE_LIMIT_ENABLED` | `false` | 是否启用限流 |
| `RATE_LIMIT_MAX_REQUESTS` | `100` | 窗口内最大请求数 |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | 限流窗口（秒） |
| `TOOLS_ENABLED` | (空) | 允许的工具名列表（逗号分隔）；为空则全部启用（可选加分项） |
| `METRICS_ENABLED` | `true` | 是否暴露 `/metrics` |
| `OTEL_ENABLED` | `false` | 是否启用 OpenTelemetry trace 导出（可选） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (空) | OTLP exporter 地址 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

`.env.example` 完整示例（见实现阶段文件）。

---

## 9. 测试计划

框架：`pytest` + `pytest-asyncio`（`asyncio_mode=auto`）+ `pytest-cov`；静态检查 `ruff` + `mypy`。

| 测试文件 | 覆盖点 |
|---|---|
| `conftest.py` | `git_repo`（初始提交）、`git_repo_multi`（多提交+分支+tag）fixture，`subprocess.run` 建临时仓库；autouse 设置 `GIT_ALLOWED_ROOTS` 并清 `get_settings` 缓存 |
| `test_config.py` | 默认值、env 覆盖、缓存清理、`ENVIRONMENT=production` 校验 |
| `test_auth.py` | `TokenVerifier`：静态 token 命中/未命中、OAuth 接入点签名 |
| `test_sandbox.py` | 白名单命中/未命中、`..` 拒绝、无白名单 fail-closed、符号链接 realpath |
| `test_security.py` | **安全专项**：Origin 存在且不在白名单 → 403、Origin 缺失放行、Host 不在白名单 → 403、`ENVIRONMENT=production` 无 token 拒绝启动、路径沙箱绕过尝试（`..`、符号链接） |
| `test_middleware.py` | 限流 429、trace_id 注入与日志 |
| `test_metrics.py` | `/metrics` 返回 Prometheus 格式、计数器随请求递增 |
| `test_git_operations.py` | 6 个工具成功路径 + 截断 + 输入边界（超界拒绝） |
| `test_server.py` | 端到端（ASGITransport）：`/health` 200、`/metrics` 200、`/mcp` 无 token 401、带 token `tools/list` 返回 6 个工具 |
| `test_protocol_compat.py` | **协议兼容**：起临时 uvicorn，用官方 `ClientSession` + `streamablehttp_client` 完成 initialize → `tools/list` → `call_tool(git_status)` |
| 性能/负载（CI 门禁，非单元） | 并发打点验证限流窗口生效、P95 延迟基准记录（脚本化） |

目标：核心模块覆盖率 ≥ 80%。

---

## 10. 客户端集成（供其他服务配置调用）

### 10.1 Claude Code / 其他 MCP Client（CLI）

```bash
claude mcp add git-server --transport http http://<host>:8000/mcp \
    --header "Authorization: Bearer <token>"
```

### 10.2 Python 客户端

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    url = "http://<host>:8000/mcp"
    headers = {"Authorization": "Bearer <token>"}
    async with streamablehttp_client(url, headers=headers) as session:
        async with ClientSession(session) as client:
            tools = await client.list_tools()
            res = await client.call_tool("git_status", {"repo_path": "/data/repos/my-project"})
            print(res)

asyncio.run(main())
```

### 10.3 通用 MCP Server JSON 配置

```json
{
  "mcpServers": {
    "git-server": {
      "type": "http",
      "url": "http://<host>:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

### 10.4 Docker 部署

```bash
docker build -t git-mcp-server servers/git-mcp-server/
docker run -d -p 8000:8000 \
  -e ENVIRONMENT=production \
  -e AUTH_TOKEN=your-secret-token \
  -e GIT_ALLOWED_ROOTS=/data/repos \
  -v /data/repos:/data/repos \
  --name git-mcp-server \
  git-mcp-server
```

> 仓库内容更新：由外部 `initContainer`（clone）+ sidecar cron（`git fetch --all --prune`）维护，Server 本身只读磁盘最新状态，与参考项目方案一致。

---

## 11. 依赖版本

### 运行时依赖（`servers/git-mcp-server/pyproject.toml`）

```
mcp>=1.13,<2.0
pydantic>=2.0.0,<3.0.0
pydantic-settings>=2.0.0,<3.0.0
uvicorn>=0.30.0,<1.0.0
starlette>=0.37.0,<1.0.0     # 直接 import，显式声明
prometheus-client>=0.20,<1.0   # /metrics
opentelemetry-sdk>=1.25,<2.0   # 可选 trace（OTEL_ENABLED）
opentelemetry-exporter-otlp>=1.25,<2.0
```

### 开发依赖（workspace 根 `[dependency-groups]`，PEP 735）

```
pytest>=8.0,<9.0
pytest-asyncio>=0.24,<1.0
pytest-cov>=5.0,<6.0
httpx>=0.27,<1.0             # 端到端 ASGI 测试用
ruff>=0.4,<1.0
mypy>=1.10,<2.0
```

---

## 12. 实施路线（评审通过后）

| 阶段 | 内容 | 产出 |
|---|---|---|
| 0 | 脚手架 | workspace 根 + `servers/git-mcp-server` 包骨架 + `uv lock`/`sync`；**核实安装版 v1 的 `token_verifier`/`AuthSettings`/`streamable_http_app` 精确 API**（`inspect.signature`） |
| 1 | 核心工具 | `config` / `errors` / `sandbox` / `executor` / `operations`（6 工具） |
| 2 | 认证与安全 | `token_verifier`（StaticTokenVerifier + OAuth 接入点）、`SecurityMiddleware`（Origin/Host/日志）、限流、`ENVIRONMENT` 强制 |
| 3 | 可观测 | `/metrics`、结构化日志、（可选）OTel |
| 4 | 服务组装 | `server.py`：FastMCP 原生认证 + `build_app`（/health、/metrics、Mount /mcp、中间件栈）+ main |
| 5 | 测试 | 11 个测试文件，`pytest` 全绿，覆盖率 ≥ 80% |
| 6 | 文档与部署 | README / `.env.example` / Dockerfile / 客户端集成示例 |

---

## 13. 风险与开放问题

| # | 风险 / 问题 | 说明 | 处置 |
|---|---|---|---|
| 1 | **v1 原生认证 API 精确路径** | `AuthSettings`/`TokenVerifier`/`StaticTokenVerifier` 的 import 路径随版本可能调整 | 实施阶段 0 `inspect.signature` 核实，以安装版为准；文档已标注 |
| 2 | Origin/Host 白名单维护 | 浏览器客户端新增域名需同步更新 `ALLOWED_ORIGINS` | 文档说明；服务间/CLI 调用无需 Origin |
| 3 | 大仓库性能 | log/blame 在超大仓库可能较慢 | timeout + 输出上限；必要时加 `--no-pager` 与行数硬限制 |
| 4 | 并发读与 fetch 竞态 | 只读工具在仓库 `git fetch` 中可能读到中间态 | 失败返回结构化错误重试；不缓存 |
| 5 | **v2 迁移（后续）** | 未来升 v2 时 `MCPServer`/`stateless_http`/`add_middleware` API 变化 | 独立任务，uv.lockfile 锁精确版本；当前设计已按无状态，迁移成本低 |
| 6 | OAuth 2.1 落地成本 | 需要外部 IdP 与客户端 token 获取流程 | 本期默认静态 token；文档给接入路径（只换 verifier），避免阻塞交付 |

---

## 附 A：与参考项目 sip-mcp-gateway 的差异对比

| 维度 | sip-mcp-gateway | 本设计（git-mcp-server） |
|---|---|---|
| SDK | 官方 `mcp` 1.x（FastMCP，无认证接线） | 官方 `mcp` 1.x（FastMCP，原生认证接线） |
| transport | SSE（已废弃） | Streamable HTTP（`streamable_http_app()`） |
| 认证 | 定义了 `auth.py` 但未接线 | SDK 原生 `token_verifier` + `AuthSettings`；production 强制 |
| Origin/Host 防护 | 无 | 规范 MUST：403 拒绝 + 防 DNS rebinding |
| 输入校验 | Pydantic 模型未接线（死代码） | `Annotated[..., Field(...)]` 真实校验 |
| 命令执行 | GitPython，无超时 | subprocess + list 参数 + timeout + 安全 env |
| 输出限制 | 字符截断（可能截断 UTF-8） | 字节截断 + 合法 UTF-8 边界 + 截断提示 |
| 可观测性 | Dockerfile 引用 `/health` 未实现 | `/health` + `/metrics` + trace_id 结构化日志 + OTel 可选 |
| 工程组织 | 单一项目 | uv workspace 单仓，多 Server 可扩展 |

---

## 附 B：两轮评审意见采纳情况

| 评审项 | 结论 | 处理 |
|---|---|---|
| ① 用 SDK 原生认证，弃自建中间件 | ✅ 采纳（v1 后期版本原生支持，无需升 v2） | §2/§3/§5.1 |
| ② 明确协议版本 + stateless | ✅ 采纳（不宣告 2026-07-28，但按无状态设计） | §2/§5.4 |
| ② Origin 校验 → 403 | ✅ 采纳（高优先级） | §5.2/§7 |
| ③ OAuth 2.1 | 🟡 采纳为生产可选项，默认静态 token | §5.1/§13 |
| ④ 指标 + 追踪 | ✅ 采纳 | §5.7/§3 |
| ⑤ 安全/负载/协议测试 | ✅ 采纳 | §9 |
| ⑥ ENVIRONMENT 强制认证 | ✅ 采纳 | §5.8/§8 |
| ⑥ uvicorn vs mcp.run() | 🟡 用 `streamable_http_app()` + uvicorn 自建 app（便于挂 /health、/metrics、中间件） | §3/§12 |
| 评审代码示例 | ⚠️ 来自社区 `fastmcp`，非官方 SDK，未采用 | — |
| 二轮：v2 稳定性 → 用 v1 | ✅ 采纳（v1 也有原生认证，锁定 v1.x） | §1.3/§2 |
