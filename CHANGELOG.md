# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，语义化版本见各子项目 `pyproject.toml`。

## [Unreleased]

### Added

- **CMDB：OTR 租户整合 + 可编辑**（`aiops-datasource-mcp-server`）
  - **数据**：业务层从空置变为完整——`enterprise` 1 / `journey` 2 / `portfolio` 11 /
    `app` 50（OTR 业务应用）+ 17 `agent`，加上原有的 10 个 k8s 服务与 10 个 codebase。
    合计 102 节点 / 97 边，`schema_version` → `1.1.0`
  - **导入器** `scripts/import_otr_tenant.py`：**幂等**，可重复执行；以
    `service-intelligence-platform-ui/tools/tenant-data/otr.json` 为准重新生成
  - **App 分源建模**（`AppAttributes.source`）：`kubernetes`（运行时字段必填）与
    `otr-inventory`（OTR 是业务盘点，**不含** owner/namespace/runtime/仓库，
    一律写 null 而不是编造）。按来源的必填由 `model_validator` 强制
  - **桥接**：10 个 k8s 服务按语义挂进 OTR 业务域（`portfolio_link`），使业务层与
    运行时层成为一张连通图。**这 10 条边是人工判断**，已记入 `derived_rules` 供编辑
  - **事件走覆盖层**：OTR 的 `topStorm` 生成 `data/cmdb-incidents-otr.json`，
    **不放进主文件**——主文件的事件桶非空会翻转 `have_incident_data`、让推断
    误以为自己有事件证据
  - **写面** `/admin/cmdb/**`：节点/边的增删改。写前**整份过 `build_graph`**，
    不过则 422 且文件逐字节不变；原子落盘（临时文件 + `os.replace`）；
    删节点**默认拒绝**被引用的节点（409 并列出挡路的边），`?cascade=true` 才级联
  - **开关** `DATASOURCE_ADMIN_ENABLED`：未设置时 development 开、**production 关**；
    未启用则**整组路由不注册**（404）；启用且有 `AUTH_TOKEN` 时逐请求校验 Bearer
  - `GET /admin/cmdb/schema` 给出每种节点类型的 attributes 表单 schema——
    `EntityDocument.model_json_schema()` **看不到** `NODE_ATTR_MODELS`（旁表），
    表单不能只有那份；`conditional_required` 另给**条件必填**（app 看 `source`），
    因为 JSON Schema 表达不了它，界面只照 `required` 渲染就漏标必填
  - **新不变量：一个 app 最多一个 `app_codebase` 边**（校验期 fail-closed）。
    `repo_by_app` 是 1:1 的 dict，多条边会让**后出现的那条静默胜出**、结果只取决于
    边在文件里的顺序；`locate_repo` 指错仓库比找不到仓库危害大。见 docs §7 第 12 条

### Fixed

- **`query_logs` 的 `total` 把「返回条数」当成「命中条数」**（`aiops-datasource-mcp-server`）
  - `_search` 只返回 `hits.hits`、把 `hits.total` 整个丢掉，于是 `total` 实际是
    `len(logs)`。真实系统里一小时可能有百万条日志，agent 看到 `total: 8` 会得出
    「窗口内只有 8 条」，而真相是「从一大堆里截了 8 条」——**返回体里没有任何字段
    能看出被截断了**
  - 现在返回 `total`（真实命中数）/ `total_relation`（`gte` = 超统计上限，total 是**下界**）
    / `returned`（本次返回条数）/ `has_more`；`hits.total` 缺失即报错，**不用默认值兜底**
  - **`by_service` / `by_level` 改为 terms 聚合**：早先是遍历返回的几十条现数，于是
    「服务分布」其实是「前 N 条里的分布」，不在前 N 条里的服务被系统性漏掉
  - 测试的 mock 一并改真实（原先只造 `hits.hits`，**比被测代码还宽松**——
    这正是缺陷溜过测试的原因）

### Added

- **`infer_candidate_services` 业务域消歧**（`aiops-datasource-mcp-server`）
  - 每个候选新增 `business_paths`（enterprise / journey / portfolio / domain 路径，
    四层键恒在）；返回体新增 `matched_domains`（输入命中到的业务域）、
    每个候选的 `in_domain`、以及跨域同分时的 `ambiguous` 标记
  - 起因：同名应用跨业务域时输出无法区分。实测 `VLMS` 同时属于 Work Order /
    Handover / Workshop（分属两个 journey），对「VLMS 打不开」返回的三个候选
    **同分、同层、reasons 一字不差**——调用方只能看名字后缀猜
  - 域内/域外**只用于排序与标注，不制造也不丢弃候选**：业务域可能录错
    （实测 `order-service` 桥接在 work-order，而「报价单」业务上属 offer-order），
    丢掉域外候选会让"域录错"表现为"服务找不到"
  - `in_domain: null`（无域线索）与 `false`（确实不在域内）**语义不同**，刻意分开

### Fixed

- **`locate_repo` 不再为没有仓库的服务编造 URL**：`_repo_url` 原有 `or service` 兜底，
  在"每个服务都有仓库"的旧数据下从不触发；OTR 的 50 个应用没有仓库，兜底会为它们
  拼出 `https://github.com/<org>/<service>`——一个不存在的地址，却长得像真的。
  现在未登记仓库时返回空串，`locate_repo` 报 `found=false` 并**区分**两种情形
  （服务不在目录中 / 服务在目录中但没有仓库）
- `_info` 把 `None` 与"键不存在"统一归到 `"unknown"`——OTR 侧的显式 null 否则会
  在摘要里渲染成字面的 `None`

### Changed

- **CMDB 测试改为对着「冻结基线」，不再钉住可编辑的活文件**
  （`tests/fixtures/cmdb-migration-baseline.json`）。实体文件一旦能被人从编辑页改，
  "断言这份文件长什么样"的测试就会在**每次编辑时**变红——而编辑正是这个功能的目的，
  这样的测试最后只会被删掉，连带把「迁移无损」那条真正的回归保护一起丢掉。
  现在分两类：数据一致性断言看**冻结基线**（`env` 夹具统一指过去），
  活文件只断言**结构与不变式**。更新基线的步骤见 docs §10

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
  - **CMDB / 拓扑**（新增）：`get_service_topology(service, hops=2)` 查 N 跳依赖拓扑
    （**方向相对起点**——上游=爆炸半径、下游=可能的上游根因）；`locate_repo(service)`
    由服务名定位仓库。数据为内置 mock（10 个服务目录 + 依赖图），**接口是生产形态**：
    换真实 CMDB 只需替换 `backends/cmdb.py` 取数实现
  - `DATASOURCE_REPO_ROOT`：本地仓库根（testbed 用 file://）；**不设默认个人路径**，
    留空走 `https://github.com/{DATASOURCE_REPO_ORG}/{repo}`
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

- **aiops-datasource-mcp-server**：CMDB 实体图谱化（**12 类节点 / 11 类边 / 4 个筛选维度**）
  - **`cmdb-entities.json` 成为 CMDB 的唯一载体**：JSON 实体文件承载服务目录、依赖拓扑、
    业务域归属；换数据只改文件，不改代码不发版。随包分发（`data/`，与 cwd 无关），
    生产用 `DATASOURCE_CMDB_PATH` 指向挂载卷
  - **ontology 由文件声明**：12 类节点（enterprise / journey / portfolio / cross_journey_hub /
    app / team / agent / tool / codebase / wiki / incident / change，未录入的类型写成显式 `[]`）、
    11 类边（参考 ontology 的 10 类 + **本地新增 `calls`**——参考图没有服务调用边，
    它的 storm fan-out 是故障扇出不是依赖）、**静态标签与派生指标强制分离**（带时间窗口的
    Top 10 指标由**校验器**硬拒写入 `tags`，不靠文档约定）
  - 新增工具 **`query_entity_graph`**（NODE TYPE / PORTFOLIO / KEY ATTRIBUTES / EDGE TYPE
    四维筛选 + 聚焦展开；facets 与计数**每次现算不存储**；未知取值 fail-closed 并区分
    「拼错」与「没数据」）与 **`infer_candidate_services`**（问题 → 候选应用：症状服务 →
    依赖拓扑扩展 → 问题文本子串匹配；每个候选带**非空的 reasons**；
    `confidence`（证据强度）与 `impact`（影响面）是**两个独立的轴**，不让"重要"冒充"可能"；
    **不给浮点分**——那会暗示一个不存在的校准模型）
  - **事件覆盖层**（`DATASOURCE_INCIDENTS_PATH`，可选）：Incident / Change 走独立只读文件，
    与主图合并后「问题 → 事件 → 应用」这条路径自动生效，**数据到位那天不需要改代码**。
    与主文件的 fail-closed **刻意相反**——主文件缺失是配置错误（返回空图会把"文件没挂上"
    洗成"这服务没依赖"），事件文件缺失只是还没数据
  - 热重载：`get_graph()` 每次调用 `stat()`，以 `(路径, mtime)` 作缓存键——改完文件
    **下次查询即生效，无需重启**（为将来的 CMDB 构建界面预留）
  - 配套 `docs/cmdb-entities.md`（schema 参考 + 校验规则 + 版本规则 + 已知失真点）与
    生成的 `docs/cmdb-entities.schema.json`（给界面用；漂移由测试守住）
  - 测试：**6 个新文件 86 个用例**——黄金迁移测试（把迁移前的 `_SERVICES` /
    `_DEPENDS_ON` 字面量冻成期望值逐字段比对）、加载器失败矩阵（缺文件 / 坏 JSON /
    主版本不符 / 缺类型键 / 派生标签 / 悬空引用 / 端点类型不符…）、四维筛选与
    fail-closed 文案、候选推断（reasons 非空 / 两轴分离 / 诚实空结果）、
    事件覆盖层（未配置不是错误 / 合并只增不改 / 事件路径真能落到 App）、schema 漂移

### Changed

- **aiops-datasource-mcp-server**：本体修订（design-v5.7 §7.1）+ 关键词匹配修复
  - **节点类型（净 12 类）**：删 `cross_journey_hub`；新增 **`domain`**（业务细域）——
    比 `portfolio` 更细，但**与它平行、各自直连 app**（不做 portfolio→domain 层级）。
    收益是**两条独立召回路径**：同一 app 被 portfolio 与 domain 分别命中即可交叉验证
  - **边类型（11 → 13）**：删 `cross_journey_link`；新增 `enterprise_journey` / `domain_link` /
    `app_codebase`（**原节点字段 `refs.repo_ref` 提升为边**，`repo_by_app` 派生索引保持同形，
    故 `locate_repo` 契约不变）；**每类边必须声明 `layer`**
  - **分层约束（enforced）**：`business` 层**禁止 app—app 边**——在 **ontology 声明层**强制，
    声明成 app—app 的边类型根本无法通过加载。`calls`（app→app）归 **runtime 层**，
    语义是观测到的运行时依赖，不是业务归属；删它会让 `get_service_topology` 失去数据源
  - **`app.attributes`** 加 `kind`（`application` / `environment`，必填）——App 节点兼表示
    应用与**其所在云环境**；加可选 `business_role`
  - **删除 6 个由 `namespace` 派生的 Portfolio**（`order`/`common`/…）——`namespace` 是
    k8s 部署分组，不是业务领域，`common` 更是装着两个 owner 完全不同的服务。
    **它们没有改名沿用**：那会把部署分组的语义残留带进业务分类
  - **节点补 `description` / `keywords`**（信封层，所有类型共有）：`description` 给 LLM 读；
    `keywords` 是**开放**自由词供文本召回，与**封闭**的 `tags`（4 个横切业务标签）职责分明——
    分开是为了保住 tags 的封闭性（挡派生指标混进静态文件）。校验：空串 / 重复项 /
    与 `tags` 重名均被拒
  - **`infer_candidate_services` 改为分层匹配**：扫**所有节点类型**（原只扫 app），
    业务层命中后沿业务边**下钻**到 app；多路径命**升一档**（交叉验证）；
    置信度按**证据类型**分档（`high` 只留给直接证据：症状服务 / 工单 `cmdb_ci` 指定）
  - **修掉 5 个"看起来在工作、实际没工作"的匹配缺陷**：
    ① `attrs.get("name")` 永远为空 → **按服务名匹配从未生效过**（名字在 `node["name"]`）；
    ② 匹配器不读 `keywords`，新字段是死数据；
    ③ 只扫 app 节点 → 业务层节点不可能被命中；
    ④ `_MIN_TEXT_MATCH_LEN = 3` 为挡 `tech="Go"` 而设，却**把 33 个双字中文关键词全杀**
    （中文是双字词密集的语言）——改为按字符类型区分：含 CJK 只需 2 字符，纯 ASCII 仍要 3；
    ⑤ 字段值**整串**匹配 → `"Java / Spring Boot"` 永远匹配不上「升级 Java 版本」，
    改为按 `/ , ; 、` 切**词元**
  - 其中缺陷 ① 长期被一个**假命题测试**掩盖：`assert any("name" in reason)` 之所以通过，
    是因为 reason 里的 `namespace` **恰好包含子串** `name`。已改为断言精确的
    `name=order-service`
  - ⚠️ **`keywords` 是派生的，不含真实用户用语**——「结账卡住」「打印结账单没反应」这类
    业务描述仍会漏召。真实召回能力要靠真实工单里的说法补齐（已登记在 `metadata.derived_rules`）
- **aiops-datasource-mcp-server**：`backends/cmdb.py` 的数据源由模块内字面量改为实体图谱文件
  - 删除 `_SERVICES` / `_DEPENDS_ON`（10 服务 / 13 条边），改读
    `EntityGraph` 上**同形的派生索引**（`services` / `depends_on` / `repo_by_app`）；
    `_info` / `_bfs` / `_repo_url` 等函数体不变
  - **对外契约零改动**：`get_service_topology` / `locate_repo` 的入参、返回键与语义
    完全不变——改动前后输出**逐字节相同**（默认与 `DATASOURCE_REPO_ROOT` 两种模式均验证）；
    `tests/test_cmdb_backend.py` **零改动通过**（迁移的验收标准）
  - 新增配置 `DATASOURCE_CMDB_PATH` / `DATASOURCE_INCIDENTS_PATH`
  - 工具数 7 → 9（`tests/test_tools.py` / `tests/test_server.py` 的工具集断言同步更新）
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
