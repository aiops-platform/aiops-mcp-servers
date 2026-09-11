# aiops-datasource-mcp-server

AIOps **数据源查询** MCP Server（Streamable HTTP）——把 Elasticsearch 日志、
Prometheus 指标、Kubernetes 状态暴露为**领域型只读工具**。

## 与 applog-mcp-server 的区别（重要）

| | applog-mcp-server | **本 server** |
|---|---|---|
| 工具形态 | **声明式透传**：`config/tools.yaml` 声明「HTTP 接口 → tool」，返回原样透传 | **领域型语义**：调用方传 `metric=cpu_percent`，**不传 PromQL 表达式** |
| 语义映射 | 无（上游接口即契约） | **内建**：哪个指标用哪条表达式由 server 决定 |
| 适合场景 | 接口本身就是对外契约 | 指标/日志的**正确查询方式是领域知识**，不该由调用方现场编写 |

设计动机：曾因映射错配（调用方传 `cpu_percent`，服务端白名单只有 `cpu`）导致
**五个指标返回同一个数字**，agent 据此得出"CPU 空闲，排除资源饱和"的错误结论。
真实数据 + 错误查询比假数据更危险——数字看似可信，语义完全错位。
详见 `multi-agent-workflow/backend/docs/design-v5.5.md` §2。

## 功能特性

- **查询必须指定时间区间与目标**：`query_logs`/`get_trace`/`query_metrics` 的
  `start_time`/`end_time` 为**必填**，fail-closed 校验（格式 / 先后 / 跨度上限），
  响应回显解析后的窗口；**不提供无窗口的全量查询**
- **语义映射住在 server 侧**：未知 metric **直接报错**并列出可用项，**不静默兜底**；
  不提供 `promql:`/`cadvisor:` 透传（刻意的收紧）
- **无数据 ≠ 0**：容器未设 limit 时百分比无定义，返回 `null` + 归因提示，
  而不是把 `+Inf`/`NaN` 当真实数字（那会被 agent 读成"内存爆了"）
- 全部工具标注 `readOnlyHint=True`（agent 侧据此自动 ALLOW）
- 上游失败归一为结构化结果 + 流式按字节截断（码点边界安全，截断**显式可见**）
- 可选 MCP Bearer 认证；`/health` 存活探针

## 工具

| 工具 | 时间区间 | 查询目标 | 后端 |
|---|---|---|---|
| `query_logs` | **必填** | `service`(可空)、`level`、`limit` | Elasticsearch |
| `get_trace` | **必填** | `trace_id`(必填) | Elasticsearch + 调用链重建 |
| `query_metrics` | **必填**（+`step_seconds`） | `service`、`metric`（5 选 1） | Prometheus `query_range` |
| `check_infra` | 无（当前状态） | `namespace`、`pod`(可空=列全部) | `kubectl get pods` |
| `describe_pod` | 无（当前状态） | `namespace`、`pod`(必填) | `kubectl describe pod` |
| `get_service_topology` | 无（静态目录） | `service`(必填)、`hops`(默认 2) | 内置 CMDB 目录 + 依赖图 |
| `locate_repo` | 无（静态目录） | `service`(必填) | 内置 CMDB 目录 |

> **CMDB / 拓扑**（后两个）查的是**静态服务目录**（谁调谁、归属哪个团队/仓库），
> 不是运行时观测数据。当前数据为 **mock**（10 个服务的内置目录），但**接口是生产形态**——
> 换真实 CMDB 只需替换 `backends/cmdb.py` 的取数实现，工具面与返回契约不变。

`metric` 可用值（**领域语义，非 PromQL**）：`cpu_percent` / `memory_percent` /
`disk_percent` / `error_rate` / `p95_latency_ms`。

`query_metrics` 返回窗口内聚合（`value` 取**峰值**）+ `min`/`max`/`avg`/`last` +
降采样 `series` + 回显 `window`。`value` 为 `null` 表示**该指标无数据**，
**不要当成 0**。

## 快速开始

前置：
- Elasticsearch（默认 `http://localhost:19200`，索引 `app-logs`）
- Prometheus（默认 `http://localhost:19090`）
- `kubectl` 且 kubeconfig 可达集群（`check_infra`/`describe_pod` 需要）

```bash
cd servers/aiops-datasource-mcp-server
cp .env.example .env
uv run python -m aiops_datasource_mcp_server     # 默认 http://127.0.0.1:8300
```

启动日志会打印已注册工具：`registered 5 tools: query_logs, get_trace, ...`

## 配置项（.env / 环境变量）

共享变量不带前缀；本 server 领域变量统一 `DATASOURCE_` 前缀。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` 下强制要求 `AUTH_TOKEN`，否则拒绝启动 |
| `BIND_HOST` / `BIND_PORT` | `127.0.0.1` / `8300` | 监听地址 |
| `AUTH_TOKEN` | (空) | 静态 Bearer Token；空则不启用 MCP 认证 |
| `DATASOURCE_ES_URL` | `http://localhost:19200` | Elasticsearch 地址 |
| `DATASOURCE_ES_INDEX` | `app-logs` | 日志索引（字段在 `app.*`：`app.service`/`app.level`/`app.@timestamp`） |
| `DATASOURCE_PROM_URL` | `http://localhost:19090` | Prometheus 地址 |
| `DATASOURCE_K8S_NAMESPACE` | `order` | `check_infra`/`describe_pod` 默认 namespace |
| `DATASOURCE_KUBECTL_BIN` | `kubectl` | kubectl 二进制 |
| `DATASOURCE_KUBECONFIG` | (空) | kubeconfig 路径；空 = kubectl 默认 |
| `DATASOURCE_REPO_ORG` | `acme-aiops` | 未配本地 root 时 `locate_repo` 返回的远端仓库组织 |
| `DATASOURCE_REPO_ROOT` | (空) | 本地仓库根目录；**非空**时 `locate_repo` 返回 `file://{root}/{repo}`（testbed 联调）；**无默认个人路径** |
| `DATASOURCE_TOPOLOGY_DEFAULT_HOPS` | `2` | `get_service_topology` 默认跳数 |
| `DATASOURCE_REQUEST_TIMEOUT_SEC` | `30.0` | 单次上游请求超时 |
| `DATASOURCE_MAX_RESPONSE_BYTES` | `1048576` | 单次返回字节上限（超出流式截断并标注） |
| `DATASOURCE_MAX_RANGE_HOURS` | `24` | 单次查询最大时间跨度（防全量扫描） |
| `DATASOURCE_DEFAULT_STEP_SEC` | `30` | `query_metrics` 默认采样步长 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 工具返回

```json
// 成功
{"metric":"cpu_percent","service":"order-service","value":1.60,"min":0.28,"avg":0.65,
 "max":1.60,"last":0.31,"series":[[1789000000,1.6]],"series_count":1,
 "window":{"start":"...","end":"...","step_seconds":30},"expr":"100 * sum(rate(...))",
 "summary":"..."}

// 无数据（诚实：null 而非 0/Inf）
{"metric":"memory_percent","value":null,"summary":"... 无数据（可能原因：容器未设 limit ...）——**不要当成 0**"}
```

失败经 MCP 以 `isError=true` 返回，文本形如
`未知 metric 'bogus'——... 可用：cpu_percent, disk_percent, error_rate, memory_percent, p95_latency_ms`。

## 客户端集成

```bash
claude mcp add aiops-datasource --transport http http://127.0.0.1:8300/mcp
```

agentflow 侧经控制面注册（详见 `multi-agent-workflow/backend/docs/design-v5.5.md` §3）：

```bash
curl -X POST localhost:8000/mcp-servers -H 'Content-Type: application/json' \
  -d '{"name":"aiops-datasource","transport":"http",
       "config":{"url":"http://127.0.0.1:8300/mcp"}}'
# 再把它绑到 agent：PUT /agent-configs/{agent} 的 mcp_server_ids
```

## 测试

```bash
cd /path/to/aiops-mcp-servers
uv run pytest servers/aiops-datasource-mcp-server/tests
uv run ruff check servers/aiops-datasource-mcp-server
uv run mypy servers/aiops-datasource-mcp-server/src/aiops_datasource_mcp_server
```

覆盖：PromQL 映射（5 键互不相同 / 未知 metric 报错 / 除零守卫）、时间区间
（非法格式 / start≥end / 超上限）、ES 查询体构造、kubectl 归一与注入防护、
**CMDB 拓扑**（方向相对起点、跳数限制、未知服务不报错、边方向、目录字段）、
**repo 定位**（远端默认 / 本地 root 覆盖 / 未收录）、工具 schema、ASGI 端到端。
上游一律用 `httpx.MockTransport`，**不触网**。
