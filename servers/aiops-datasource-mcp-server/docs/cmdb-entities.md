# CMDB 实体图谱（`cmdb-entities.json`）

**实体文件是 CMDB 的唯一载体。** 服务目录、依赖拓扑、业务域归属全部来自它；
换数据只需改文件，不需要改代码、不需要发版。

配套文件：

| 文件 | 用途 |
|---|---|
| `src/aiops_datasource_mcp_server/data/cmdb-entities.json` | **唯一载体**（随包分发） |
| `docs/cmdb-entities.schema.json` | JSON Schema —— 编辑器侧校验。⚠️ **不含**每种节点类型的 `attributes` 模型（`Node.attributes` 是 `dict[str, Any]`，`NODE_ATTR_MODELS` 是旁表），表单 schema 要走 `GET /admin/cmdb/schema` |
| `src/aiops_datasource_mcp_server/backends/cmdb_admin.py` | **写路径**：节点/边的增删改，写前全量校验、原子落盘（见 §10） |
| `scripts/import_otr_tenant.py` | OTR 租户导入器（幂等）。见 §5.1 |
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
  "schema_version": "1.1.0",
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
| **业务** | `enterprise` | Enterprise（企业功能） | **1**（OTR） |
| | `journey` | Journey（用户旅程） | **2** |
| | `portfolio` | Portfolio（业务领域） | **11** |
| | `domain` | Domain（业务细域） | **1**（手工录入，OTR 无此层） |
| **应用** | `app` | App（应用／云环境） | **60**（50 业务盘点 + 10 k8s 服务，见 §5.5） |
| **支撑** | `agent` | 智能体 | **17**（无 `agent_watches` 边，OTR 未给监视对象） |
| **支撑** | `team` / `tool` | 团队 / 工具 | 0 |
| **工程** | `codebase` / `wiki` | 代码仓库 / 知识文档 | **10** / 0 |
| **事件** | `incident` / `change` | 故障 / 变更 | 0（**刻意留空**，见 §6 与 §5.5） |

合计 102 节点 / 97 边（`schema_version` 1.1.0）。

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

⚠️ **`app_codebase` 是 1:1 的**——一个 app 最多一条。它给 `locate_repo` 供数
（`repo_by_app` 是 dict，1:1），多条边会静默取最后一条。校验器强制，见 §7 第 12 条。

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

### 5.1 业务层的来历：先删掉假的，再录入真的（OTR 租户）

**第一阶段（2026-09-15）：删掉那批假的。** 曾经的 6 个 Portfolio
（`order` / `common` / `payment` / `inventory` / `logistics` / `account`）全部删除——
它们由 `attributes.namespace` 派生，而 `namespace` 是 **k8s 部署分组**、不是**业务领域**。
最明显的是 `common`：它装着两个 owner 完全不同的服务（`notification-service`
「平台基础」和 `audit-service`「安全合规」），业务上是个杂物筐；6 个里 4 个还是单例。

> **为什么是删除而不是改名沿用**：改名会把「部署分组」的语义残留带进业务分类——
> 那比空着更糟，因为它会让人以为业务域已经理过了。

**第二阶段（2026-09-16）：录入真的。** OTR 租户的业务结构导入进来：

| | 数量 | 来源 |
|---|---|---|
| `enterprise` | 1（OTR） | `snapshot.enterprise` |
| `journey` | 2 | `snapshot.journeys` |
| `portfolio` | 11 | `snapshot.portfolios` |
| `domain` | 1（`domain:work-order-ops`） | **整合前手工录入的，OTR 无 domain 层，原样保留** |
| `app` | 50（业务盘点）+ 10（k8s 服务） | 见 §5.5 |

导入脚本：`scripts/import_otr_tenant.py`（**幂等**，可重复执行回基线）。

```bash
# 重新导入（otr.json 更新后照此重跑）
python3 scripts/import_otr_tenant.py --dry-run
python3 scripts/import_otr_tenant.py
```

**业务层 `keywords` 的来源要分清**：OTR **不提供**任何检索词，所以那里没有"以谁为准"
的冲突。脚本把**整合前人工撰写的**中文问题视角词（`portfolio:warranty` 的「理赔」
「索赔」、`portfolio:work-order` 的「工单」「售后订单」…）**合并保留**下来。这些词是
§2 里那条"按用户怎么描述故障来写"的直接产物，丢掉它们等于把业务语言召回一起丢掉
——实测「理赔申请没反应」正是靠它们才落到 `portfolio:warranty` 下。

### 5.5 ⚠️ App 有两类来源，`source` 是判别字段

整合后 `app` 桶里有两个语义层级不同的群体，**`attributes.source` 区分它们**：

| `source` | 数量 | 有运行时字段吗 | 有仓库吗 |
|---|---|---|---|
| `kubernetes` | 10 | ✅ namespace/owner/tier/runtime/criticality | ✅ `aiops-test-*` 等 10 个 |
| `otr-inventory` | 50 | ❌ **全为 null** | ❌ **没有** |

**为什么 OTR 侧的运行时字段是 null 而不是填上默认值**：那份数据是**业务盘点**，
根本不含 owner / namespace / runtime / 仓库——连 `snapshot.facts.teams: 10` 都是
`round(50 / 5.2)` 算出来的**假数**。填默认值会让 `get_service_topology` 开始自信地
返回假归属。**未知就写 null**，由 `AppAttributes` 的按来源校验器强制（见 §7）。

**OTR 真正带来的是**：业务分级 `business_tier`（1/2/3）、`operational_status`、
`incidents_total` / `change_count` / `risk_score`、技术栈 `tech`，以及
`holiday_critical` / `finance_freeze` 两个横切标签。

> ⚠️ **`business_tier` 与 `tier` 不是同一根轴**：OTR 的 1/2/3 是**业务分级**，
> CMDB 的 `tier` 是**拓扑层**（edge/core/support）。把 T1 写进 `tier` 会被 Literal
> 直接拒——这是刻意的（见 §4「`tier` 与 `tier1` 是两个东西」）。

**桥接**：10 个 k8s 服务在业务层原本只有 2 个有归属，脚本按语义给其余 8 个补了
`portfolio_link`（映射表在 `scripts/import_otr_tenant.py` 的 `BRIDGE`）。**这 10 条边
是全图里唯一一处非来源数据**，已写进 `metadata.derived_rules.bridge_to_runtime_apps`，
在编辑页面上可以直接改。

**为什么不能按字面"以 otr.json 为准"删掉那 10 个服务**：OTR **完全没有**调用拓扑
（50 个 app 之间**零条边**）与仓库信息。删掉这一层，`get_service_topology` 与
`locate_repo` 会同时失去数据源，而 OTR 补不上——比"数据不够全"更糟的是"能力归零"。

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

**OTR 的事件数据就在这里**：`data/cmdb-incidents-otr.json`（导入脚本一并生成），
装的是 `snapshot.topStorm` 那一次风暴（`INC-MBR-77104`，159 条子告警，
落点 `app:xentry-workshop`）。**默认不加载**——要把 `DATASOURCE_INCIDENTS_PATH`
指过去才生效。

> ⚠️ **别把它挪进主文件。** 主文件的 `incident` / `change` 桶**必须保持为空**：
> `graph_query` 用"事件桶是否非空"判断 `have_incident_data`，一旦非空就启用事件匹配
> 并把 `degraded` 翻成 False——等于拿一次风暴冒充整份事件语料，让
> `infer_candidate_services` 以为自己有事件证据。
> `tests/test_cmdb_entities_data.py` 有两条测试分别钉住"主文件为空"与"覆盖层里有数据"。

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
12. **一个 app 最多一个 `app_codebase` 边**（`cmdb._repo_url` 依赖此不变量，见下）
13. 每个边类型**必须声明 `layer`**（缺了就无法执行第 6 条）

### 第 12 条：为什么"一个 app 一个仓库"要 fail-closed

`repo_by_app` 是 **1:1 的 dict**，按 `repo_by_app[app.name] = codebase.name` 赋值。
同一个 app 挂两条 `app_codebase` 时，**后出现的那条静默胜出**——不报错、不提示，
结果只取决于边在文件里的顺序。哪天顺序一变（重新导入、在编辑页删了一条又加回来），
`locate_repo` 指向的仓库就跟着变，而且**看不出发生过什么**。

`locate_repo` 是"照着结果去翻代码"的工具：给错仓库比不给仓库危害大得多。

> 真要多仓库（monorepo 拆分）再说——那需要先把 `repo_by_app` 改成一对多的结构、
> 让 `locate_repo` 返回多个。不是把两条边塞进一个只能装一个值的 dict。

**配套**：编辑页（§10）必须能**删边**，否则用户被这条约束挡住后只能回去开终端。

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
| `DATASOURCE_ADMIN_ENABLED` | 未设置 | CMDB 写端点开关。未设置 = development 开、**production 关**。见 §10 |

**热重载**：`get_graph()` 每次调用都 `stat()` 一次文件，以 `(路径, mtime)` 作缓存键。
改完文件**下一次查询即生效，无需重启**——这是给将来的 CMDB 构建界面留的。

---

## 10. 写路径与编辑端点（`/admin/cmdb`）

**只读的 MCP 面（`/mcp`）不变**；写入走一组独立挂在 ASGI 上的 HTTP 端点。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/admin/cmdb/schema` | 表单 schema（ontology + 每种类型的 attributes 模型） |
| GET | `/admin/cmdb/summary` | 当前图概况（每次写后返回，用于确认改动生效） |
| GET | `/admin/cmdb/nodes` | 节点清单（可选 `?type=`） |
| POST | `/admin/cmdb/nodes` | 新增节点 |
| PUT | `/admin/cmdb/nodes/{id}` | 改节点（局部更新） |
| DELETE | `/admin/cmdb/nodes/{id}` | 删节点（默认不级联，`?cascade=true` 才连边一起删） |
| POST | `/admin/cmdb/edges` | 新增边（`id` 可省，按 `e:<type>:<from>-><to>` 生成） |
| DELETE | `/admin/cmdb/edges/{id}` | 删边 |

> **建边时端点类型由 ontology 约束，不要在客户端复刻那套规则。** 前端只需读
> `GET /admin/cmdb/schema` 的 `edge_types`：按 `from_types` / `to_types` 判断当前节点
> 在该边的哪一端，并把另一端的选择范围**过滤到允许的类型**——102 个节点里挑一个，
> 就此缩成 10 个 codebase 里挑一个，而且选不出非法组合。合法性仍由服务端判定。

### 三条不可动摇的规矩

1. **写前整份过一遍 `build_graph`，不过就整个不落盘。** 不设"轻量校验"这条捷径——
   一条指向不存在节点的边、一个 id 前缀与 type 不符的节点**不会在读取时炸**，它会让
   `get_service_topology` 返回一份看着正常的错答案。返回 422，文件**逐字节不变**。
2. **删节点默认拒绝**（有边引用时返回 409 并列出是哪些边）。被引用说明图里还有别的
   结构依赖它，静默级联会删掉用户没打算删的东西。要级联就显式声明，且删了哪些边
   **如实返回**。
3. **原子落盘**（临时文件 + `os.replace`）。进程在写一半时被杀，留下的是旧文件而不是
   半截 JSON——半截文件会让 CMDB 工具 fail-closed，整个诊断链跟着挂。

### 开关与认证

- **未启用时整组路由不注册**（404），不是"注册了再返回 403"——不存在的路径更难被绕过。
- 启用且有 `AUTH_TOKEN` 时，逐个请求校验 `Authorization: Bearer`。
- **默认按环境**：development 开、**production 关**。一个能改诊断数据源的写端点，
  不该因为"部署时忘了关"而暴露；要开就显式 `DATASOURCE_ADMIN_ENABLED=true`，
  且 `production` 下必须有 `AUTH_TOKEN`（启动校验强制）。

### 热重载是端到端成立的

写完显式清一次 `clear_graph_cache()` / `clear_overlay_cache()`——不依赖文件系统时间戳
的粒度（同 ns 内的两次写入在粗粒度 fs 上可能拿到同一个 mtime，那时缓存会返回上一版）。
`get_service_topology` / `infer_candidate_services` 等工具**下一次调用**就能看到改动。

### 数据可编辑之后，测试该怎么摆

**这是做编辑器的必然推论，不是可选项。**

实体文件一旦能被人改，"断言这份文件长什么样"的测试就会在**每次编辑时**变红——而编辑
正是这个功能的目的。这样的测试最后只会被人删掉，连带把"迁移无损"那条真正的回归保护
一起丢掉。

所以分成两类，各自看**不同的数据**：

| 测试 | 看哪份数据 | 断言什么 |
|---|---|---|
| `test_cmdb_entities_data.py` 的「迁移无损」三条 | `tests/fixtures/cmdb-migration-baseline.json`（**冻结**） | 逐字段、逐条边 |
| 行为测试（拓扑方向、图查询、派生索引） | 同上（`env` 夹具统一指过去） | 语义正确性 |
| 「文件本身」几条 | 包内**活文件** | **只断言结构与不变式**，不钉数量 |

基线是**导入器的产出快照**——`scripts/import_otr_tenant.py` 跑完那一刻的状态，
不含其后的人工编辑。要更新它，是明确决定"把当前状态认定为新基线"，**不是为了让测试变绿**：

```bash
git show HEAD:.../cmdb-entities.json > /tmp/base.json
python3 scripts/import_otr_tenant.py --cmdb /tmp/base.json
cp /tmp/base.json servers/aiops-datasource-mcp-server/tests/fixtures/cmdb-migration-baseline.json
```

> ⚠️ 别把基线改成指向包内活文件——那等于把"可编辑"这个前提撤销掉。
> 也别因为活文件变了就改基线：活文件本来就该变。
