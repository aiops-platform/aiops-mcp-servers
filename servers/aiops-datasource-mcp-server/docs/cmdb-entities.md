# CMDB 实体图谱（`cmdb-entities.json`）

**实体文件是 CMDB 的唯一载体。** 服务目录、依赖拓扑、业务域归属全部来自它；
换数据只需改文件，不需要改代码、不需要发版。

配套文件：

| 文件 | 用途 |
|---|---|
| `src/aiops_datasource_mcp_server/data/cmdb-entities.json` | **唯一载体**（随包分发） |
| `docs/cmdb-entities.schema.json` | JSON Schema —— 给将来的 CMDB 构建界面做表单生成与编辑器侧校验 |
| `src/aiops_datasource_mcp_server/backends/entity_graph.py` | 加载 + 校验 + 派生索引 |
| `src/aiops_datasource_mcp_server/backends/graph_query.py` | 图查询与候选推断（纯函数） |

> **运行时校验走 pydantic，不走 JSON Schema。** pydantic 的错误信息更好，而且能表达
> JSON Schema 表达不了的规则：id 前缀必须与 `type` 一致、引用完整性、标签词表、
> 边端点的类型相容。JSON Schema 是给界面用的。两者由
> `tests/test_schema_export.py` 的漂移测试绑定。

---

## 1. 顶层结构

```json
{
  "schema_version": "1.0.0",
  "metadata": { "source": "...", "derived_rules": { ... } },
  "ontology": { "node_types": [...], "edge_types": [...],
                "key_attributes": [...], "derived_metrics": [...] },
  "nodes": { "app": [...], "portfolio": [...], ... },
  "edges": [...]
}
```

**`nodes` 是按类型分键的对象，12 个键恒存在。** 未录入的类型写成显式 `[]`，不能省略键——
省略会让「没数据」看起来像「这个类型不存在」。

`ontology` 是**声明**：它定义节点类型、边类型、边方向与允许的属性词表。边方向声明在
**边类型上**（一次），不在每条边上重复。

---

## 2. 节点（12 类）

| 类型键 | 名称 | 视角 | 本 CMDB 已录入 |
|---|---|---|---|
| `enterprise` | Enterprise | business | 0 |
| `journey` | Journey | business | 0 |
| `portfolio` | Portfolio | business | **6** |
| `cross_journey_hub` | Cross-Journey hub | business | 0 |
| `app` | App | application | **10** |
| `team` | Team | organization | 0 |
| `agent` | Agent | organization | 0 |
| `tool` | Tool | organization | 0 |
| `codebase` | Codebase | engineering | **10** |
| `wiki` | Wiki | engineering | 0 |
| `incident` | Incident | operations | 0（见 §6 覆盖层） |
| `change` | Change | operations | 0（见 §6 覆盖层） |

### 节点 envelope

```json
{
  "id": "app:order-service",
  "type": "app",
  "name": "order-service",
  "display_name": null,
  "attributes": { "namespace": "order", "owner": "交易履约", "tier": "core",
                  "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "critical" },
  "tags": ["tier1"],
  "refs": { "repo_ref": "codebase:aiops-test-order-service" },
  "notes": null
}
```

- **`id` 带类型前缀**（`<type>:<slug>`），校验前缀必须等于 `type`。工具边界会剥掉前缀，
  所以 `get_service_topology("order-service")` 这类调用不受影响。
- **`name` 是自然键**，同类型内唯一。
- **`attributes` 按类型校验**（pydantic，`extra="forbid"`）。目前只有 `app` 有严格模型：
  - `tier` ∈ `edge` / `core` / `support`
  - `criticality` ∈ `critical` / `high` / `medium` / `low`
- **`tags`** 只能取 `ontology.key_attributes` 里声明的**静态**标签（见 §4）。
- **`refs`** 是类型化指针（**不是边**）。目前用于 App → Codebase。

> **`refs` 为什么不是边**：参考 ontology 的 10 类边里**没有** App→Codebase 关系
> （Codebase 是个没有任何命名边指向它的节点类型）。用受校验的引用字段表达，将来
> 若确认它确实是边，提升为真边是 schema-**minor** 改动。

---

## 3. 边（11 类）

| 类型键 | 名称 | 方向 | 端点 |
|---|---|---|---|
| `journey_link` | Journey link | 无向 | journey — portfolio |
| `portfolio_link` | Portfolio link | 无向 | portfolio — app |
| `cross_journey_link` | Cross-journey link | 无向 | journey — cross_journey_hub |
| `support` | Support · team + wiki | 无向 | app — team / app — wiki |
| `agent_watches` | Agent watches | 有向 | agent → app |
| `tool_integrates` | Tool integrates | 有向 | tool → app / agent |
| `storm_fan_out` | Storm fan-out | 有向 | incident → incident |
| `change_causes_incident` | Change → incident | 有向 | change → incident |
| `incident_cluster_app` | Incident cluster → app | 有向 | incident → app |
| `change_cluster_app` | Change cluster → app | 有向 | change → app |
| **`calls`** | Calls (dependency) | 有向 | app → app，属性 `relation ∈ {http, rpc, mq}` |

前 10 类照搬参考 ontology；**`calls` 是本地新增的第 11 类**——参考图里**没有**服务调用边
（它的 `Storm fan-out` 是故障扇出，不是调用依赖），而这条是我们有且不能丢的。

**无向边两个方向都合法**（`portfolio — app` 与 `app — portfolio` 等价）。

`support` 合并了 Team 与 Wiki 两类支撑物（照参考图的形状），用
`attributes.support_kind ∈ {team, wiki}` 区分。将来若要拆成两条边，是 minor 改动。

---

## 4. 横切标签（4 个静态）与派生指标（3 个）—— 必须分开

### 静态标签 `key_attributes`

`tier1`（一级系统）· `holiday_critical`（节假日关键）· `finance_freeze`（财务冻结期）·
`manhattan_wms_spotlight`（WMS 关注焦点）

这些是**人工声明的分类**，可以打在任意类型的节点上。

### 派生指标 `derived_metrics`

`top10_incidents_18m` · `top10_changes_13m` · `emergency_changes`

**它们只被声明，没有任何实例化槽位。** 带时间窗口（18M / 13M）的指标会随时间漂移，
写进静态文件就变成会过期的存储态；查询时现算。

**这条规则由校验器强制，不是文档约定**：loader 构建合法标签集时只取 `key_attributes`，
节点 `tags` 里出现派生键 → 硬报错（带专门文案）。所以将来的构建界面即使有 bug，也
无法把派生指标写进文件。

### ⚠️ `tier` 与 `tier1` 是两个东西

| 字段 | 含义 | 取值 |
|---|---|---|
| `attributes.tier` | **拓扑层**：在调用图里处于哪一层 | `edge` / `core` / `support` |
| `tags: ["tier1"]` | **业务断言**：是不是一级系统 | 有 / 无 |

`tier: "edge"` 意思是"调用图的入口层"（如网关），**不是**"不是一级系统"。
一个渲染界面如果把两者都显示成 "Tier"，就会产出错误的数据。

---

## 5. 本文件的派生规则（见 `metadata.derived_rules`）

当前 10 个 App、6 个 Portfolio、10 个 Codebase 里有**两处是派生来的**，不是外部录入的。
写在这里是为了让 review 的人知道它们的来源。

### 5.1 Portfolio ← `namespace`

**6 个 Portfolio 全部由 `attributes.namespace` 派生**：`order`(4) / `common`(2) /
`payment`(1) / `inventory`(1) / `logistics`(1) / `account`(1)。

> ⚠️ **这层映射有语义落差，将来必须修**：`namespace` 是 **k8s 部署分组**，不是参考
> ontology 里的**业务域**。最明显的是 `common`——它装着两个 owner 完全不同的服务
> （`notification-service`「平台基础」和 `audit-service`「安全合规」），语义上是个杂物筐。
> 6 个 Portfolio 里 4 个是单例。
>
> 录入真实业务域时应重新划分，并同时调整 `portfolio_link` 边。

### 5.2 `tier1` ← `criticality == "critical"`

`order-service` 与 `payment-service` 带 `tags: ["tier1"]`。

**它不是 `criticality` 的另一种写法**：`criticality` 是四级技术属性
（critical/high/medium/low），`tier1` 是二元业务断言。将来"一级系统"若改为包含
`high`，两者需要解耦——所以它们是两个字段，而不是一个映射的两种展示。

### 5.3 `owner` 为什么还是自由文本

10 个 `owner` 值与 10 个服务**一一对应、零共享**（「网关团队」「交易履约」「支付团队」…）。
把它们提升为 Team 节点只会造出 10 个单例团队，看着像组织架构但提供不了任何新的遍历能力，
且我们**没有真实的团队标识**（团队 id / 值班 / 联系方式），派生一个 id 就是造数据。

将来有真实 Team 数据时是**纯追加改动**：加 `team:*` 节点 + `support` 边，join 约定为
`team.attributes.display_label == app.attributes.owner`。`support` 边类型已经声明好了。

### 5.4 7/10 的仓库在本地不存在

`gateway-service` / `order-service` / `warranty-service` 的 `repo` 指向
`aiops-test-*`（testbed 里真实存在）；另外 7 个指向的仓库本地没有。

**这不是 CMDB 事实，是环境事实**——在别的环境里结论会反过来，所以**不写进文件**。
需要的话在查询时算（`DATASOURCE_REPO_ROOT` 非空时用 `Path(root, repo).is_dir()`）。

---

## 6. 事件覆盖层（Incident / Change）

`DATASOURCE_INCIDENTS_PATH` 指向一个**可选**文件，与主文件同 schema，但：

- 用同一个 loader，`require_all_node_types=False`（只需含 `incident` / `change` 等桶）
- 复用主图的 `ontology`，不重复声明
- 边**可以指向主图节点**（`incident → app:order-service`），端点校验会把主图节点算作"存在"
- 合并是**只增不改**：节点 id 或边三元组与主图冲突即报错（那说明两份数据对同一件事
  有不同说法，静默覆盖会藏掉一个真实的不一致）

### 与主文件刻意相反的一条

| | 主文件（`DATASOURCE_CMDB_PATH`） | 事件文件（`DATASOURCE_INCIDENTS_PATH`） |
|---|---|---|
| 未配置 | 用包内默认文件 | **没有事件数据——合法状态** |
| 配了路径但读不到 | 配置错误，报错 | 配置错误，报错（路径写错了） |
| 缺失时的行为 | fail-closed 抛 `CONFIG_ERROR` | 返回 `None`，走 `degraded` 标记 |

**为什么主文件要 fail-closed**：返回空图会把"文件没挂上"洗成"这服务没有依赖"——
`get_service_topology` 返回 `known=False`，agent 就会自信地报告一份**错误的诊断**。
报错严格优于一份看着正常的错答案。

**为什么这不适用于事件文件**：没有事件数据是**当前系统的真实状态**，不是配置错误。
把它当错误会让整个工具不可用。

事件数据到位后，「问题 → 事件 → 应用」这条路径**不需要改代码**就会自动生效
（`graph_query._EXPANSION_EDGE_TYPES` 已经包含事件边，推断里的事件匹配段会自动启用）。

---

## 7. 校验规则

加载时逐条校验，任一失败即 `CONFIG_ERROR` 并指出**具体是哪个节点/边/字段**：

1. 文件存在、可读、UTF-8，`json.loads` 成功（报错带行列号）
2. `schema_version` 主版本 == 1
3. 顶层与 `ontology` 的 `extra="forbid"`（打错的键不会静默忽略）
4. `nodes` 的类型键集合 == `ontology.node_types` 声明的集合
5. `ontology` 自洽：`key_attributes` 与 `derived_metrics` 的 key 互斥
6. 逐节点：id 格式 / 前缀等于 type / 全局唯一 / `(type, name)` 唯一 / 属性合模型
7. `tags` ⊆ 静态标签（派生键专报错）
8. `refs` 指向存在的节点
9. 逐边：id 唯一 / 类型已声明 / 端点存在 / 端点类型相容（无向边允许反向）/ 属性在词表内 /
   `(type, from, to)` 不重复
10. `calls` 子图只引用已知 app（`cmdb._bfs` 依赖此不变量）

**不做环检测**：真实 CMDB 可能有环，`_bfs` 已用 `dist` memo 正确处理。

---

## 8. 版本规则

单一 `schema_version`（semver）。加载器规则：

- `major != 1` → 拒绝加载，报错说明文件版本、支持的版本、需要迁移
- `minor` / `patch` 差异 → 静默接受（向前兼容的新增）

| 变更 | 版本 |
|---|---|
| 新增节点类型 / 边类型 / 可选字段 / 静态标签 | minor |
| 改变字段含义、删除字段、改变 `id` 格式、在 `attributes` 与 `tags` 之间挪概念 | **major** |

---

## 9. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DATASOURCE_CMDB_PATH` | 空 | 空 = 用包内 `data/cmdb-entities.json`（与 cwd 无关）。生产指向挂载卷 |
| `DATASOURCE_INCIDENTS_PATH` | 空 | 空 = 无事件数据（合法）。见 §6 |

**热重载**：`get_graph()` 每次调用都 `stat()` 一次文件，以 `(路径, mtime)` 作缓存键。
改完文件**下一次查询即生效，无需重启**——这是给将来的 CMDB 构建界面留的。
