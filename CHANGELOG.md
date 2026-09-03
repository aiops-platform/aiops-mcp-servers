# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，语义化版本见各子项目 `pyproject.toml`。

## [Unreleased]

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
