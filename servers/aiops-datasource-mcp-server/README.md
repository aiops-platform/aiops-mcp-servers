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
| `get_service_topology` | 无（静态图谱） | `service`(必填)、`hops`(默认 2) | CMDB 实体图谱 `calls` 子图 |
| `locate_repo` | 无（静态图谱） | `service`(必填) | CMDB 实体图谱 |
| `query_entity_graph` | 无（静态图谱） | `node_types`/`portfolios`/`key_attributes`/`edge_types`/`node_id`+`hops` | CMDB 实体图谱（全 13 类边） |
| `infer_candidate_services` | 无（静态图谱） | `problem`(必填)、`services`(强烈建议)、`namespaces`、`max_hops`、`limit` | CMDB 实体图谱 + 推断 |

> **CMDB 图谱**（后四个）查的是**静态实体图谱**（谁调谁、归属哪个业务域/仓库、有哪些事件），
> 不是运行时观测数据。数据来自实体文件 `data/cmdb-entities.json`——**它是 CMDB 的唯一载体**，
> 换数据只需改文件：`DATASOURCE_CMDB_PATH` 指向自己的文件即可，工具面与返回契约不变。
> schema / 校验规则 / 边类型 / 版本规则见 **[`docs/cmdb-entities.md`](docs/cmdb-entities.md)**。

**`query_entity_graph` 与 `get_service_topology` 的分工**——两者在 2 跳以上会给出不同结果，
**这是故意的**：

- `get_service_topology` 只走 `calls` 一种边，方向**相对起点**定义，把"上游的其他下游"
  （兄弟节点）排除在外。判断**爆炸半径 / 根因**用它。
- `query_entity_graph` 跨全部 13 类边、按四个维度筛选，聚焦时按**无向邻域**展开
  （因为 `portfolio_link` 等边没有方向）。用途是**探索结构**。

**`infer_candidate_services`** 由问题描述推断候选应用。四类证据：

| 证据 | 说明 |
|---|---|
| **E1 症状服务** | 调用方已确认的服务名（**强烈建议传**）+ 沿 `calls` 的拓扑扩展 |
| **E2 分层关键词匹配** | 扫**所有节点类型**：app 的 name/`keywords`/属性字段，以及**业务层**（journey/portfolio/domain）——命中后沿业务边**下钻**到 app |
| **E3 事件** | 命中 Incident/Change 后沿事件边落点（**当前无事件数据，恒不生效**） |
| **E4 交叉验证** | 同一 app 被多条独立路径命中 → **升一档** |

**置信度按证据类型分档**，不是按命中位置——`high` 只留给**直接证据**（症状服务 /
工单 `cmdb_ci` 指定）；识别性命中（name/`keywords`）、`domain` 下钻、拓扑邻居是 `medium`；
`portfolio`/`journey`/`enterprise` 下钻、只命中**共享属性**（tech/owner/namespace）是 `low`。

> 为什么把"只命中共享属性"压到 `low`：6 个服务都跑 Java、3 个都在 order namespace，
> **"命中"只说明它在那个集合里，不说明它与故障有关**。「升级 Java 版本」会正确列出所有
> Java 服务，但它们是 `low` 而不是 `high`。

每个候选带**非空的 `reasons`** 与 `hit_paths` / `matched_layers`（交叉验证的依据）。
**不给浮点分**——那会暗示一个不存在的校准模型。`confidence`（证据强度）与 `impact`
（影响面）是**两个独立的轴**——不让"重要"冒充"可能"。**未命中任何服务时请勿编造服务名。**

> ⚠️ **召回能力取决于 `keywords` 的质量，而当前 `keywords` 是派生的**（由服务名/owner/tech
> 机械翻译，不含真实用户用语）。用户说「结账卡住」时仍可能匹配不上——**真实召回要靠
> 真实工单里的说法补齐**。

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

启动日志会打印已注册工具：`registered 9 tools: query_logs, get_trace, ...`

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
| `DATASOURCE_CMDB_PATH` | (空) | **CMDB 实体图谱文件（CMDB 的唯一载体）**；空 = 用包内 `data/cmdb-entities.json`（与 cwd 无关）。**缺失即 fail-closed 报错**——不返回空图 |
| `DATASOURCE_INCIDENTS_PATH` | (空) | 可选的事件覆盖文件（Incident / Change）；空 = **暂无事件数据（合法状态）**。配了路径却读不到才算配置错误。OTR 租户的用 `data/cmdb-incidents-otr.json` |
| `DATASOURCE_ADMIN_ENABLED` | (未设置) | CMDB 写端点（`/admin/cmdb/**`）开关。未设置 = development 开、**production 关**；打开了 `production` 下强制要有 `AUTH_TOKEN` |
| `DATASOURCE_REQUEST_TIMEOUT_SEC` | `30.0` | 单次上游请求超时 |
| `DATASOURCE_MAX_RESPONSE_BYTES` | `1048576` | 单次返回字节上限（超出流式截断并标注） |
| `DATASOURCE_MAX_RANGE_HOURS` | `24` | 单次查询最大时间跨度（防全量扫描） |
| `DATASOURCE_DEFAULT_STEP_SEC` | `30` | `query_metrics` 默认采样步长 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## CMDB 编辑端点（`/admin/cmdb/**`）

只读的 MCP 面（`/mcp`）不变；写入是一组**独立挂在 ASGI 上**的 HTTP 端点，
给"在页面上改 CMDB"用。详见 `docs/cmdb-entities.md` §10。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/admin/cmdb/schema` | 表单 schema（ontology + 每种节点类型的 attributes 模型） |
| GET | `/admin/cmdb/summary` | 当前图概况（每次写后返回） |
| GET | `/admin/cmdb/nodes` | 节点清单（可选 `?type=`） |
| POST | `/admin/cmdb/nodes` | 新增节点 |
| PUT | `/admin/cmdb/nodes/{id}` | 改节点（局部更新） |
| DELETE | `/admin/cmdb/nodes/{id}` | 删节点（默认拒绝被引用的；`?cascade=true` 才级联） |
| POST | `/admin/cmdb/edges` | 新增边 |
| DELETE | `/admin/cmdb/edges/{id}` | 删边 |

```bash
# 本地起服务（development 下默认可用）
python -m aiops_datasource_mcp_server

curl -s localhost:8300/admin/cmdb/summary | python3 -m json.tool
curl -s -X PUT localhost:8300/admin/cmdb/nodes/portfolio:campaign \
     -H 'Content-Type: application/json' -d '{"display_name":"营销活动"}'
```

**写前整份过 `build_graph`**：不合法的改动返回 422 且**文件逐字节不变**；
校验通过才原子落盘。改完**下一次查询即生效**（MCP 工具无需重启）。

> ⚠️ **未启用时这组路径根本不存在**（404）。默认只在 development 可用；
> 想在 production 打开要显式设 `DATASOURCE_ADMIN_ENABLED=true` 并配 `AUTH_TOKEN`。
> 它没有 CSRF 防护，打开后建议只在内网/本机监听。

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

CMDB 实体图谱另有三组：

| 测试 | 守什么 |
|---|---|
| `test_cmdb_entities_data.py` | **黄金迁移测试**——把迁移前的 `_SERVICES` / `_DEPENDS_ON` 字面量冻成期望值，逐字段比对。`test_catalog_is_rich_enough` 只抽查 `owner != "unknown"`，一个属性字符串打错能溜过去；这个不会 |
| `test_entity_graph_backend.py` | 加载器**失败矩阵**——缺文件、坏 JSON、主版本不符、缺类型键、派生标签、悬空引用、端点类型不符…每条校验配一个反例 |
| `test_graph_query_backend.py` | 四个维度、facets 现算、未知值 fail-closed（且区分"拼错"与"没数据"）、聚焦展开 |
| `test_inference_backend.py` | 候选推断——reasons 非空、置信/影响两轴分离、空结果被如实标记 |
| `test_incidents_overlay.py` | 事件覆盖层——未配置不是错误、合并只增不改、事件路径真的能落到 App |
| `test_schema_export.py` | `docs/cmdb-entities.schema.json` 与 pydantic 模型的**漂移** |
