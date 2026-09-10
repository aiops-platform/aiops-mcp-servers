# aiops-mcp-servers

AIOps 平台 MCP Servers 单仓（monorepo）——基于官方 `mcp` SDK（Python / FastMCP）的独立可部署 MCP Server 集合，采用 [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) 统一管理。

每个 server 都是独立的 Python 包（`servers/*`），可单独打包、部署与配置调用。

## Servers

| Server | 说明 | 文档 |
|--------|------|------|
| [git-mcp-server](servers/git-mcp-server/README.md) | 只读访问 Git 仓库（status / log / show / branches / grep / blame） | [README](servers/git-mcp-server/README.md) |
| [applog-mcp-server](servers/applog-mcp-server/README.md) | 声明式把多个 HTTP 日志查询接口注册成只读 MCP tool（config/tools.yaml） | [README](servers/applog-mcp-server/README.md) |
| [aiops-datasource-mcp-server](servers/aiops-datasource-mcp-server/README.md) | 领域型数据源查询：ES 日志 / Prometheus 指标 / K8s 状态；**查询必须带时间区间与目标** | [README](servers/aiops-datasource-mcp-server/README.md) |

> 更多 MCP server 后续按 `servers/<name>/` 目录添加。
>
> **两种工具取向**：`applog` 是**声明式透传**（上游接口即契约）；`aiops-datasource`
> 是**领域型语义**（映射内建，调用方传 `metric=cpu_percent` 而非 PromQL）。
> 指标/日志的"正确查询方式"属领域知识，透传给 LLM 现场编写会引入语义错配——
> 详见 [aiops-datasource README](servers/aiops-datasource-mcp-server/README.md) 与
> `multi-agent-workflow/backend/docs/design-v5.5.md`。

## 目录结构

```
aiops-mcp-servers/
├── pyproject.toml             # uv workspace 虚拟根 + 共享 dev 工具
├── uv.lock
├── docs/                      # 跨 server 设计文档
├── CHANGELOG.md
└── servers/
    ├── git-mcp-server/        # 只读 Git MCP server
    │   ├── pyproject.toml     # 独立运行时依赖
    │   ├── Dockerfile
    │   ├── src/git_mcp_server/
    │   └── tests/
    └── applog-mcp-server/     # 声明式 HTTP 日志查询 MCP server
        ├── pyproject.toml
        ├── config/tools.yaml  # ★ 工具声明（HTTP 接口 → MCP tool）
        ├── src/applog_mcp_server/
        └── tests/
```

## 快速开始

```bash
# 同步全部包（含 dev 工具）
uv sync --all-packages

# 启动 git-mcp-server（详见其 README 配置 GIT_ALLOWED_ROOTS）
cd servers/git-mcp-server && cp .env.example .env
uv run python -m git_mcp_server
```

## 开发约定

- Python >= 3.11，代码风格遵循根 `pyproject.toml` 中 ruff / mypy 配置
- 运行时依赖声明在各 server 自己的 `pyproject.toml`；共享开发工具（pytest / ruff / mypy）在根 `[dependency-groups]`
- 测试：`uv run pytest <server>/tests`（覆盖点见各 server README）
- 变更记录：`CHANGELOG.md`（Keep a Changelog 格式）

## 文档

- [git-mcp-server 设计文档](docs/git-mcp-server-design.md) — 架构决策、工具规格与安全设计

## License

Apache-2.0，见 [LICENSE](LICENSE)。
