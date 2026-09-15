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

| 层 | 类型键 | 名称 | 本 CMDB 已录入 |
|---|---|---|---|
| **业务** | `enterprise` | Enterprise（企业功能） | 0 |
| | `journey` | Journey（用户旅程） | 0 |
| | `portfolio` | Portfolio（业务领域） | **0** ⚠️ 见 §5.1 |
| | `domain` | Domain（业务细域） | 0 |
| **应用** | `app` | App（应用／云环境） | **10** |
| **支撑** | `team` / `agent` / `tool` | 团队 / 智能体 / 工具 | 0 |
| **工程** | `codebase` / `wiki` | 代码仓库 / 知识文档 | **10** |
| **事件** | `incident` / `change` | 故障 / 变更 | 0（见 §6 覆盖层） |

**业务层四类的层级**（收敛树，逐层变细）：

```
Enterprise ──→ Journey ──→ Portfolio ──┐
                                      ├──→ App
                            Domain ───┘
```

⚠️ **`domain` 与 `portfolio` 平行，不是它的子级。** 两者各自直连 App，
边规则与 `tool` / `codebase` 同构。收益是**两条独立的召回路径**——同一 App 被两条路径
分别命中即可交叉验证；且它们的 `terms` 各写各的，互不挤占。

### 节点 envelope

```json
{
  "id": "app:order-service",
  "type": "app",
  "name": "order-service",
  "display_name": null,
  "description": "订单主流程服务：接收下单请求，编排支付/库存/定价/售后等下游调用",
  "attributes": { "kind": "application", "namespace": "order", "owner": "交易履约",
                  "tier": "core", "tech": "Java / Spring Boot", "runtime": "k8s",
                  "criticality": "critical" },
  "tags": ["tier1"],
  "keywords": ["order-service", "order", "订单", "下单", "交易履约", "Java", "Spring Boot"],
  "refs": {},
  "notes": null
}
```

- **`id` 带类型前缀**（`<type>:<slug>`），校验前缀必须等于 `type`。工具边界会剥掉前缀，
  所以 `get_service_topology("order-service")` 这类调用不受影响。
- **`name` 是自然键**，同类型内唯一。
- **`description`**（信封层，**所有类型共有**）：一句话说明**这个节点是干什么的**，
  给 LLM 判断相关性用。与 `display_name` 同类，所以不放各类型的 attributes 里——
  避免每个类型各定义一个同名同义的字段。
- **`attributes` 按类型校验**（pydantic，`extra="forbid"`）：
  - **`app`**：`kind` ∈ `application` / `environment`（**必填**，见下）；
    `tier` ∈ `edge` / `core` / `support`；`criticality` ∈ `critical` / `high` / `medium` / `low`；
    `business_role` 可选。
  - **`enterprise` / `journey` / `portfolio` / `domain`**：只有 `capability`
    （可选，**当前空置**——业务语义待录入，见 §5.1）。
- **`tags`**：**封闭**词表，只能取 `ontology.key_attributes` 里声明的横切业务标签（见 §4）。
- **`keywords`**：**开放**自由词，供关键词召回命中。见下。
- **`refs`** 是类型化指针（不是边）。**当前全部为空**——原先唯一的用途
  （`repo_ref` → Codebase）已于 2026-09-15 提升为 `app_codebase` 边，见 §3。

#### `tags` 封闭 vs `keywords` 开放——为什么分成两个字段

| | `tags` | `keywords` |
|---|---|---|
| 词表 | **封闭**（`ontology.key_attributes` 声明的 4 个） | **开放**（任意词） |
| 语义 | 横切业务标签，**有语义、可枚举** | 只为"能被问题描述命中"，**不承载语义** |
| 用途 | 筛选器可穷举、可作为流程判据 | 文本召回 |
| 稀疏性 | **大多数节点天然没有标签**——这是对的 | 每个节点都该有 |

**为什么不全塞进 `tags`**：`tags` 的封闭性是**故意的**——当初设它就为了挡住派生指标
（Top 10 by incidents 那几个带时间窗口的）混进静态文件，也为了"tier1 到底是什么"
有个唯一的权威答案。开放它等于放弃这两条。

校验上还做了两件事，防职责混淆：

1. **空串与重复项被拒**——空串能匹配任何文本，会把该节点灌进所有查询结果
2. **同一个词不得既在 `tags` 又在 `keywords`**——否则"该按标签筛还是按关键词搜"说不清；
   发现即报，逼人明确选一边

#### `app.kind`：应用与云环境共用一类节点

按 v5.7 的约定，App 节点既表示**具体实现某个业务域的应用/服务**，**也表示它所在的云环境**
（如 `azure-cn-north3`）。`kind` 用来区分两者——判断相关性时能据此排除环境节点：
工单问的通常是服务，不是机房。

#### 业务语义的三个字段，用途不同不能合并

| 字段 | 位置 | 用途 | 谁消费 | 为什么不能合并 |
|---|---|---|---|---|
| `keywords` | **信封层** | **检索键**——用户/工单里实际会说的词 | 确定性关键词召回 | 要的是**高召回**：宁可多捞，词可以很粗 |
| `description` | **信封层** | **判断依据**——这业务域干什么、边界在哪 | LLM 推理 | 要的是**高精度**：需要语义，不是关键词 |
| `capability` | attributes | 业务能力名（Journey 层用） | 展示与归类 | 给人和 UI 看 |

> ⚠️ **业务层的 `keywords` 该怎么写**：不由"这个业务域是什么"决定，而由
> **"用户会怎么描述它出问题"**决定。写「售后服务选择」是业务视角、不会有人这么说；
> 写「退货」「换货」「申请售后没反应」才是问题视角——**只有后者能被工单命中**。

**另一个陷阱**：`terms` 是 v5.7 **撤掉**的字段名（已统一到信封层的 `keywords`）。
写 `terms` 会被属性模型**硬拒**——这是故意的，否则写了它的人会以为检索词生效了，
实际是个死字段。

---

## 3. 边（13 类）与**分层约束**

| layer | 类型键 | 名称 | 方向 | 端点 |
|---|---|---|---|---|
| **business** | `enterprise_journey` | Enterprise journey | 无向 | enterprise — journey |
| | `journey_link` | Journey link | 无向 | journey — portfolio |
| | `portfolio_link` | Portfolio link | 无向 | portfolio — app |
| | `domain_link` | Domain link | 无向 | domain — app |
| **runtime** | **`calls`** | Calls (dependency) | 有向 | app → app，属性 `relation ∈ {http, rpc, mq}` |
| **support** | `app_codebase` | App → codebase | 无向 | app — codebase |
| | `support` | Support · team + wiki | 无向 | app — team / app — wiki |
| | `agent_watches` | Agent watches | 有向 | agent → app |
| | `tool_integrates` | Tool integrates | 有向 | tool → app / agent |
| **event** | `incident_cluster_app` | Incident cluster → app | 有向 | incident → app |
| | `change_cluster_app` | Change cluster → app | 有向 | change → app |
| | `change_causes_incident` | Change → incident | 有向 | change → incident |
| | `storm_fan_out` | Storm fan-out | 有向 | incident → incident |

**无向边两个方向都合法**（`portfolio — app` 与 `app — portfolio` 等价）。

`support` 合并了 Team 与 Wiki 两类支撑物（照参考 ontology 的形状），用
`attributes.support_kind ∈ {team, wiki}` 区分。将来若要拆成两条边，是 minor 改动。

### `layer` 与 business 层约束（**enforced，不是文档约定**）

每类边**必须声明 `layer`**，它把「app 与 app 之间不能直接关联，要通过业务域」这条规则
变成**可校验的约束**：

```
business 层：禁止 app — app
```

**校验发生在声明层**：loader 拒绝任何 `layer == "business"` 且 `from_types` / `to_types`
同时含 `app` 的边类型——任何数据都必然违规，与其等坏数据进来再报错，不如让这种声明
根本无法通过。

**为什么 `calls`（app → app）不算违规**：它归 **runtime 层**，语义是**观测到的运行时依赖
事实**，不是业务归属。删掉它，`get_service_topology`（爆炸半径 / 上游根因）立即失去
数据源——**分层把"业务归属"与"运行时依赖"分开，而不是一刀切禁掉 app—app**。

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

当前 10 个 App、10 个 Codebase 里有**两处是派生来的**，不是外部录入的。
写在这里是为了让 review 的人知道它们的来源。

### 5.0 `description` 与 `keywords` —— **全部为派生**

20 个节点的 `description` 与 `keywords` **不是外部录入的**：

| 字段 | 怎么来的 |
|---|---|
| `description` | 由 `tech` + `owner` + `tier` + **调用图中的位置**合成 |
| `keywords` | 由 `name` / `owner` **机械派生**；中文词是服务名与 owner 的忠实中译 |

⚠️ **技术栈词（`Java` / `Go` / `Spring Boot` / `Python` …）已从 `keywords` 移除。**
它们是**共享属性**（6 个服务都跑 Java、3 个都在 order namespace），由 `tech` 字段匹配
即可；留在 `keywords` 里会让「命中 tech」被当成**识别性命中**，进而把 6 个服务全抬一档。
匹配器据此把"只命中共享属性"压到 `low` 置信——**"命中"只说明它在那个集合里，
不说明它与故障有关**。

所以 `keywords` 里现在只放**说明它是谁**的词：服务名、服务名变体、中文翻译、owner 标签。

> ⚠️ **它们不含任何真实用户用语。** `keywords` 里的「订单」「支付」「库存」是从
> `order-service` / `payment-service` / `inventory-service` **翻译**过来的，
> **不是**从真实工单里统计出来的。
>
> 所以：**业务语言的召回仍然会落空。** 用户说「结账卡住」「单子下不了」时，
> 这些词一个都不在 `keywords` 里。**真正的召回能力要靠真实工单里的说法补齐**——
> 这是本文件当前最大的已知缺口。

### 5.1 ⚠️ 业务层是完全空置的——那批 Portfolio **已被删除**

**曾经的 6 个 Portfolio**（`order` / `common` / `payment` / `inventory` / `logistics` /
`account`）**已于 2026-09-15 全部删除**。它们是由 `attributes.namespace` 派生的。

删除的原因是**它们语义上是错的**：`namespace` 是 **k8s 部署分组**，不是**业务领域**。
最明显的是 `common`——它装着两个 owner 完全不同的服务（`notification-service`
「平台基础」和 `audit-service`「安全合规」），业务上是个杂物筐。6 个里 4 个还是单例。

> **为什么是删除而不是改名沿用**：改名会把「部署分组」的语义残留带进业务分类——
> 那比空着更糟，因为它会让人以为业务域已经理过了。

**现状**：`enterprise` / `journey` / `portfolio` / `domain` **四类节点数均为 0**，
业务语义（`description` / `terms` / `capability`）全部待录入。

**这意味着**：当前任何基于业务层的召回都会落空，只能靠 App 级真实字段
（`name` / `namespace` / `owner` / `tech`）与 `calls` 拓扑。**业务域录入是提升
「问题 → 服务」定位能力的前提**，schema 已就位（见 §2 的业务层属性）。

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
6. **`ontology` 自洽：business 层不得声明 app—app 边**（§3 的分层约束）
7. 逐节点：id 格式 / 前缀等于 type / 全局唯一 / `(type, name)` 唯一 / 属性合模型
8. `tags` ⊆ 静态标签（派生键专报错）
9. `keywords`：不得含空串或重复项；**不得与 `tags` 重名**（职责混淆）
10. `refs` 指向存在的节点
10. 逐边：id 唯一 / 类型已声明 / 端点存在 / 端点类型相容（无向边允许反向）/ 属性在词表内 /
    `(type, from, to)` 不重复
11. `calls` 子图只引用已知 app（`cmdb._bfs` 依赖此不变量）
12. 每个边类型**必须声明 `layer`**（缺了就无法执行第 6 条）

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
