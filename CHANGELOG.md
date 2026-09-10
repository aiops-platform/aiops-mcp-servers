# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，语义化版本见各子项目 `pyproject.toml`。

## [Unreleased]

### Added

- **aiops-datasource-mcp-server**（新增独立可部署 MCP Server）
  - **领域型**只读工具：`query_logs` / `get_trace` / `query_metrics` / `check_infra` / `describe_pod`
    ——调用方传领域语义（`metric=cpu_percent`），**不传 PromQL 表达式**；语义映射住在 server 侧
  - **查询必须指定时间区间与目标**：三个带时间的工具 `start_time`/`end_time` 为必填，
    fail-closed 校验（ISO8601 格式 / start<end / 跨度上限 `DATASOURCE_MAX_RANGE_HOURS`），
    响应回显解析后的 `window`；**不提供无窗口的全量查询**
  - **未知 metric 直接报错并列出可用项，不做静默兜底**；不提供 `promql:`/`cadvisor:` 透传
    （设计动机：曾因白名单命名错配致五个指标返回同一数字，agent 据此误判"CPU 空闲"）
  - **无数据 ≠ 0**：容器未设 limit 时百分比无定义，返回 `null` + 归因提示，
    而非把 `+Inf`/`NaN` 当真实数字（会被 agent 读成"内存爆了"）
  - 上游调用：流式按字节截断（码点边界安全 + **显式截断后缀**）、显式超时、
    `follow_redirects`、非 2xx/超时归一为结构化结果；`query_metrics` 用 `query_range`（有窗口）
  - kubectl 封装：命令白名单（只 get/describe）、`create_subprocess_exec` 非 shell、
    namespace/pod 字符校验；kubectl 缺失归一为 `CONFIG_ERROR`
  - 可选原生 Bearer 认证（`AUTH_TOKEN`，production 强制）；`/health` 存活探针
  - 配置：共享变量不前缀，领域变量统一 `DATASOURCE_*`（对齐 applog 的 `APPLOG_`）；端口 `8300`
  - 测试：6 个文件 46 用例（PromQL 映射互异/未知报错/除零守卫、时间区间四种非法形态、
    ES 查询体与 trace 重建、kubectl 注入防护、工具 schema 必填校验、ASGI 端到端含错误路径）；
    上游一律 `httpx.MockTransport` 不触网

- **applog-mcp-server**（新增独立可部署 MCP Server）
  - 声明式把多个 HTTP 日志查询接口注册成只读 MCP tool：`config/tools.yaml` 里一段 = 一个 tool，新增查询只改 YAML
  - 基于官方 `mcp` SDK（FastMCP 1.x）的 Streamable HTTP 服务，`json_response` 模式，入参由声明生成（必填/可选真实校验）
  - 支持 GET(query 参数) / POST(query + JSON body)；工具统一标注 `readOnlyHint`；`/health` 存活探针
  - 入参支持 `in: path` 动态路径模板（如 `/api/sip-aiops/app-log/chain/{requestId}`，须必填、值 URL 转义）；默认 `config/tools.yaml` 含 `query_chain_log_by_request_id` 示例
  - 上游调用：流式读取 + 字节上限截流（内存有界、码点边界安全截断）、显式超时、`follow_redirects`、非 2xx/超时归一为结构化失败
  - 可选原生 Bearer 认证（`AUTH_TOKEN`，fail-closed）：production 强制要求（`create_mcp_server`/`build_app` 亦校验）
  - 服务端执行日志：成功 debug / 失败与认证拒绝 warning（含 method/url/status/耗时），便于运维观测
  - fail-closed 配置校验：tools.yaml 非法/工具名与入参名非合法标识符或 Python 保留字/名字重复/`in:body` 配 GET/path 含 query/base_url 非 http(s) → 拒绝启动并报中文错误
  - 测试：5 个测试文件（34 个用例）覆盖 loader 校验、HTTP 透传/4xx/超时/截断/重定向/码点边界、ASGI 端到端（/health、认证、tools/list、tools/call）

### Changed

- **git-mcp-server**：`repo_path` 支持「项目名」定位，免绝对路径
  - 新增 `resolve_repo_ref`：`repo_path` 可传绝对路径（`GIT_ALLOWED_ROOTS` 内，`~` 自动展开）或**项目名**
    （某 allowed root 的 basename / 一级子目录名）；裸名两遍匹配（root 名 → 一级子目录），解析结果 realpath
    后仍须落在白名单 root 内（防 symlink 越界），未命中返回 `PERMISSION_DENIED`（中文报错列出可用名）
  - 修复 `os.path.realpath` 不展开 `~` 的潜伏 bug（roots 与输入统一 `expanduser + realpath`）
  - `.env` / `.env.example`：`GIT_ALLOWED_ROOTS` 默认并列 `acc-aiops-platform-zjb` + `acc-aiops-platform`
    两棵树（多 root 逗号分隔，同名冲突按列表顺序取先者）
  - 工具 schema 与 server instructions 同步说明两态定位语义
  - 测试：`conftest` 抽 `_make_git_repo` 并新增 `container_repo` / `two_roots` fixture；sandbox 增 7 用例
    （root 名 / 子目录名 / 第二棵树 / 未知名 / `~` 展开 / symlink 越界），git operations 增 3 个 name e2e

## [0.1.0] - 2026-09-02

### Added

- **git-mcp-server**（新增独立可部署 MCP Server）
  - 基于官方 `mcp` SDK（FastMCP）的 Streamable HTTP 服务，端点 `/mcp`（json_response 模式）
  - 6 个纯只读 Git 工具：`get_repo_status`、`get_commit_log`、`get_commit_detail`、`list_branches`、`search_code`、`blame_file`
  - 工具实现采用 subprocess + list 参数执行 git（无 shell 注入），porcelain/NUL 机器可读格式解析
  - 原生 Bearer 认证：FastMCP `token_verifier`，配置 `AUTH_TOKEN` 后 `/mcp` 强制校验（fail-closed）
  - 安全中间件 `SecurityMiddleware`：Host / Origin 白名单校验（防 DNS rebinding，MCP 规范 MUST）+ trace_id 注入与请求日志，仅作用于 `/mcp`；`/health`、`/metrics` 保持公开
  - 限流中间件 `RateLimitMiddleware`：TokenBucket 按客户端 IP 限流，超限返回 429 + `Retry-After`（默认关闭）
  - 路径沙箱：仓库白名单 + symlink 解析，越界返回 `PERMISSION_DENIED`；非 git 仓库返回 `INVALID_REQUEST`
  - 资源限制：git 命令显式超时（默认 30s）+ 输出字节上限（1MB）+ 安全环境变量（`GIT_TERMINAL_PROMPT=0` 等）
  - 统一配置（pydantic-settings）：环境变量 / `.env`，生产环境强制认证与仓库白名单
  - 可观测性：Prometheus `/metrics`（请求量、耗时、工具错误、git 命令指标）+ 结构化请求日志
  - 工具动态开关：`TOOLS_ENABLED` 可按需启用子集
  - 部署产物：`Dockerfile`（多阶段构建 + 非 root + HEALTHCHECK）、`.env.example` 配置模板
  - 测试：11 个测试文件（56 个用例），覆盖配置/认证/沙箱/安全中间件/限流/指标/工具行为/MCP 协议端到端，全绿
  - 文档：`docs/git-mcp-server-design.md` 设计文档、`servers/git-mcp-server/README.md`（架构图、启动日志、配置表、客户端集成、安全设计）
