"""CMDB 实体图谱：实体文件（JSON）的加载、校验与派生索引。

**实体文件是 CMDB 的唯一载体**。本模块把它读成内存图，并在加载时构建两个与历史
实现同形的派生索引（``services`` / ``depends_on``），使 ``backends/cmdb.py`` 的对外
契约完全不变——这是"迁移无损"的实现手段。

## 加载路径在调用时解析（不是 import 时）

``tools/datasource.py`` 有明确约束：FastMCP 用 ``get_type_hints`` 解析注解，所以**工具
参数默认值**必须在 import 时冻成模块级常量。但那条约束只适用于注解，不适用于这里——
``get_graph()`` 在**每次工具调用时**重新解析 settings 并 ``stat()`` 一次：

- 换环境变量（``DATASOURCE_CMDB_PATH``）后 ``get_settings.cache_clear()`` 立即生效，
  测试无需任何额外机制；
- 文件改动后**下一次查询即生效，无需重启**——这是给将来的 CMDB 构建界面留的热重载。

## 缺文件为什么 fail-closed

返回空图会把"配置错了"洗成一份**看着正常的错误答案**：``get_service_topology`` 会
返回 ``known=False``，agent 就会自信地报告"该服务没有依赖、不在 CMDB"。这比报错更糟。
故缺失即抛 ``CONFIG_ERROR``——但**只在 CMDB 工具内部抛**（见 ``cmdb.py`` 的取数点），
不在 import 或进程启动时抛，免得打挂 /health 与无关的数据面工具。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

#: 本模块支持的 schema 主版本。主版本不符 = 拒绝加载（需迁移）。
SUPPORTED_SCHEMA_MAJOR = 1

_DATA_PACKAGE = "aiops_datasource_mcp_server"
_DATA_FILE_PARTS = ("data", "cmdb-entities.json")

#: 节点 id 形如 ``app:order-service``——前缀必须与节点的 ``type`` 一致。
NODE_ID_RE = re.compile(r"^(?P<type>[a-z_]+):(?P<slug>[A-Za-z0-9._-]+)$")

#: ``calls`` 边允许的 relation 词表（由 ontology 声明，此处仅为缺省兜底）。
_DEFAULT_CALLS_RELATIONS = ("http", "rpc", "mq")


class GraphLoadError(AppError):
    """实体文件加载/校验失败。统一映射为 ``CONFIG_ERROR``，交由错误处理层呈现。"""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(ErrorCode.CONFIG_ERROR, message, details)


# ======================================================================
# Schema（pydantic）—— 导出 JSON Schema 给将来的 CMDB 构建界面用
# ======================================================================
class NodeTypeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    label_zh: str
    view: Literal["business", "application", "organization", "engineering", "operations"]


class EdgeTypeSpec(BaseModel):
    """边类型的声明。**方向性、端点类型与所属层在这里声明一次**，不在每条边上重复。"""

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    label_zh: str
    directed: bool
    from_types: list[str]
    to_types: list[str]
    #: 该边类型允许的属性词表：{属性名: 允许值}。空 dict = 不允许多余属性。
    attributes: dict[str, list[str]] = Field(default_factory=dict)
    origin: Literal["reference", "local"]
    #: 所属层。**business 层内禁止 app—app 边**（见 build_graph 的分层约束）——
    #: 这是「app 与 app 之间不能直接关联，要通过业务域」这条规则的强制点。
    #:   business —— 业务归属（enterprise → journey → portfolio/domain → app）
    #:   runtime  —— 观测到的运行时依赖事实（calls），**不是业务归属**
    #:   support  —— 支撑关系（codebase / team / wiki / agent / tool）
    #:   event    —— 事件落点（incident / change）
    layer: Literal["business", "runtime", "support", "event"]


class KeyAttributeSpec(BaseModel):
    """**静态**横切标签（人工声明的分类）。派生指标见 ``DerivedMetricSpec``。"""

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    label_zh: str


class DerivedMetricSpec(BaseModel):
    """**派生**指标——只声明，**没有任何实例化槽位**。

    故意不带 ``values`` 之类的字段：派生指标随时间窗口变化（见 ``window_months``），
    写进静态文件就会变成会漂移的存储态。查询时现算（``graph_query.facets``）。
    """

    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    window_months: int | None
    source: str
    computed_by: str


class Ontology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_types: list[NodeTypeSpec]
    edge_types: list[EdgeTypeSpec]
    key_attributes: list[KeyAttributeSpec]
    derived_metrics: list[DerivedMetricSpec]


class AppAttributes(BaseModel):
    """App 节点的属性（前 6 个逐字段迁移自历史 ``_SERVICES``，``kind`` 为 v5.7 新增）。

    ``tier`` 是**拓扑层**（edge/core/support），与静态标签 ``tier1``（一级系统）是
    两个不同概念——名字像，含义无关。

    ``kind`` 区分「应用」与「云环境」：按 v5.7 的约定，App 节点既表示具体实现某个业务域的
    应用/服务，**也表示它所在的云环境**（如 ``azure-cn-north3``）。判断相关性时能据此
    排除环境节点——工单问的通常是服务，不是机房。

    ``business_role`` 可选：一句话说明这个应用承担什么业务职能，供 LLM 判断时参考。
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["application", "environment"]
    namespace: str
    owner: str
    tier: Literal["edge", "core", "support"]
    tech: str
    runtime: str
    criticality: Literal["critical", "high", "medium", "low"]
    business_role: str | None = None


class BusinessDomainAttributes(BaseModel):
    """业务层节点（enterprise / journey / portfolio / domain）**特有的**属性。

    描述与检索词在**信封层**（``description`` / ``keywords``），所有节点类型共有——
    不在这里重复定义。这里只放业务层独有的概念。

    全部字段可选——**业务语义层当前是空置的**（见实体文件的 ``derived_rules``），
    这些字段是给将来的构建界面 / 人工录入填的。**schema 先定死，数据留空**。

    ⚠️ **业务层的 ``keywords`` 该怎么写**：不由"这个业务域是什么"决定，而由
    **"用户会怎么描述它出问题"**决定。写「售后服务选择」是业务视角、不会有人这么说；
    写「退货」「换货」「申请售后没反应」才是问题视角——只有后者能被工单命中。
    """

    model_config = ConfigDict(extra="forbid")
    #: 业务能力名（Journey 层用，如参考站的 "Engage"）。
    capability: str | None = None


#: 按节点类型标注属性模型。未登记的类型属性开放（留给将来的界面自由扩展）。
NODE_ATTR_MODELS: dict[str, type[BaseModel]] = {
    "app": AppAttributes,
    "enterprise": BusinessDomainAttributes,
    "journey": BusinessDomainAttributes,
    "portfolio": BusinessDomainAttributes,
    "domain": BusinessDomainAttributes,
}


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    name: str
    display_name: str | None = None
    #: 一句话说明**这个节点是干什么的**。所有节点类型共有（与 ``display_name`` 同类），
    #: 放在信封层而不是各类型的 attributes 里——避免每个类型各定义一个同名同义的字段。
    #: 主要给 LLM 判断相关性用。
    description: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    #: **封闭**词表：只能取 ``ontology.key_attributes`` 里声明的横切业务标签
    #: （Tier 1 / Holiday-critical …）。**大多数节点天然没有标签是对的**——横切业务
    #: 标签本来就只有少数系统够得上。想放任意关键词请用 ``keywords``。
    tags: list[str] = Field(default_factory=list)
    #: **开放**的自由关键词，供关键词召回命中。与 ``tags`` 职责分明：
    #:   tags     —— 封闭的横切业务标签，有语义、可枚举、能被筛选器穷举
    #:   keywords —— 开放的自由词，只为"能被问题描述命中"，不承载语义
    #: 分开是为了保住 tags 的封闭性——当初设封闭正是为了挡住派生指标
    #: （Top 10 by incidents 那几个带时间窗口的）混进静态文件。
    keywords: list[str] = Field(default_factory=list)
    #: 类型化指针。**当前全部为空**——原先唯一的用途（``repo_ref`` → Codebase）
    #: 已于 v5.7 提升为 ``app_codebase`` 边。
    refs: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    id: str
    type: str
    from_: str = Field(alias="from")
    to: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = ""
    #: 记录"哪些字段是派生来的"，让 review 的人知道它不是外部录入的数据。
    derived_rules: dict[str, Any] = Field(default_factory=dict)


class EntityDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    metadata: Metadata = Field(default_factory=Metadata)
    #: 覆盖层（事件文件）可省略 ontology——由主图提供，避免两份声明漂移。
    ontology: Ontology | None = None
    nodes: dict[str, list[Node]] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)


# ======================================================================
# 加载后的内存图
# ======================================================================
@dataclass(frozen=True)
class EntityGraph:
    path: str
    schema_version: str
    ontology: Ontology
    metadata: Metadata
    #: node_id → 节点原始 dict
    nodes: dict[str, dict]
    #: 边的原始 dict（含 "from"/"to"）
    edges: list[dict]
    nodes_by_type: dict[str, list[str]]
    #: app 名 → 目录 dict（**与历史 ``_SERVICES`` 同形，含 repo**）
    services: dict[str, dict]
    #: app 名 → [(callee_name, relation)]（**与历史 ``_DEPENDS_ON`` 同形**）
    depends_on: dict[str, list[tuple[str, str]]]
    #: app 名 → 仓库标识（经 ``refs.repo_ref`` 解引用）
    repo_by_app: dict[str, str]
    key_attribute_keys: frozenset[str]
    derived_metric_keys: frozenset[str]

    # ---- 便捷访问 ----------------------------------------------------
    def node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def app_by_name(self, name: str) -> dict | None:
        for nid in self.nodes_by_type.get("app", []):
            n = self.nodes[nid]
            if n["name"] == name:
                return n
        return None

    def edge_types_of(self, *types: str) -> list[dict]:
        want = set(types)
        return [e for e in self.edges if not want or e["type"] in want]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def merged_with(self, overlay: EntityGraph | None) -> EntityGraph:
        """返回**主图 + 覆盖层**的只读合并视图（主图本身不变）。

        覆盖层只增不改：节点 id 或边三元组与主图冲突即报错——那说明两份数据对同一件事
        有不同说法，静默覆盖会藏掉一个真实的不一致。
        """
        if overlay is None or (not overlay.nodes and not overlay.edges):
            return self

        dup_ids = sorted(set(self.nodes) & set(overlay.nodes))
        if dup_ids:
            raise GraphLoadError(
                f"事件覆盖层与主图存在重复节点 id：{dup_ids}——覆盖层只应新增事件节点，"
                f"不应重定义主图节点。",
                {"node_ids": dup_ids},
            )
        existing = {(e["type"], e["from"], e["to"]) for e in self.edges}
        for edge in overlay.edges:
            triple = (edge["type"], edge["from"], edge["to"])
            if triple in existing:
                raise GraphLoadError(
                    f"事件覆盖层与主图存在重复的边：{triple}", {"edge": list(triple)}
                )
            existing.add(triple)

        nodes = {**self.nodes, **overlay.nodes}
        edges = [*self.edges, *overlay.edges]
        nodes_by_type = {k: list(v) for k, v in self.nodes_by_type.items()}
        for type_key, ids in overlay.nodes_by_type.items():
            nodes_by_type.setdefault(type_key, []).extend(ids)

        services, depends_on, repo_by_app = _derive_indexes(nodes, nodes_by_type, edges)
        return EntityGraph(
            path=self.path,
            schema_version=self.schema_version,
            ontology=self.ontology,
            metadata=self.metadata,
            nodes=nodes,
            edges=edges,
            nodes_by_type=nodes_by_type,
            services=services,
            depends_on=depends_on,
            repo_by_app=repo_by_app,
            key_attribute_keys=self.key_attribute_keys,
            derived_metric_keys=self.derived_metric_keys,
        )


# ======================================================================
# 路径解析与缓存
# ======================================================================
def resolve_cmdb_path() -> str:
    """主实体文件路径。settings 留空时回落到**包内**自带文件（与 cwd 无关）。"""
    configured = (get_settings().datasource_cmdb_path or "").strip()
    if configured:
        return configured
    return str(resources.files(_DATA_PACKAGE).joinpath(*_DATA_FILE_PARTS))


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime_ns: int) -> EntityGraph:  # noqa: ARG001 (mtime 仅作缓存键)
    """按 (路径, mtime) 缓存。文件改动 → mtime 变 → 自然重载，无需重启。"""
    return _load_path(path)


def get_graph() -> EntityGraph:
    """取当前 CMDB 图。**每次调用都 stat 一次**，因此配置与文件改动立即生效。

    文件缺失/不可读 → ``GraphLoadError``（fail-closed，见模块 docstring）。
    """
    path = resolve_cmdb_path()
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise GraphLoadError(
            f"CMDB 实体文件不可读：{path}（{exc.strerror}）。"
            f"请检查 DATASOURCE_CMDB_PATH，或确认包内 data/cmdb-entities.json 已随包安装。",
            {"path": path},
        ) from exc
    return _load_cached(path, stat.st_mtime_ns)


def clear_graph_cache() -> None:
    """清空加载缓存（测试用）。"""
    _load_cached.cache_clear()


def load_entity_graph(
    path: str | None = None,
    *,
    ontology: Ontology | None = None,
    require_all_node_types: bool = True,
    external_nodes: dict[str, str] | None = None,
) -> EntityGraph:
    """显式加载实体文件（**不走缓存**）。

    ``path=None`` 等价于 ``get_graph()``（走缓存）。显式路径用于：测试（tmp_path）、
    以及事件覆盖层（``require_all_node_types=False`` + 复用主图 ``ontology``）。

    ``external_nodes``（id → type）传入主图节点表，使覆盖层的边可以指向主图节点。
    """
    if path is None:
        return get_graph()
    return _load_path(
        str(path),
        ontology=ontology,
        require_all_node_types=require_all_node_types,
        external_nodes=external_nodes,
    )


# ======================================================================
# 加载 + 校验
# ======================================================================
def _load_path(
    path: str,
    *,
    ontology: Ontology | None = None,
    require_all_node_types: bool = True,
    external_nodes: dict[str, str] | None = None,
) -> EntityGraph:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphLoadError(
            f"CMDB 实体文件读不出：{path}（{exc.strerror}）", {"path": path}
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphLoadError(
            f"CMDB 实体文件不是合法 JSON：{path} 第 {exc.lineno} 行第 {exc.colno} 列 — {exc.msg}",
            {"path": path, "line": exc.lineno, "col": exc.colno},
        ) from exc
    return build_graph(
        data,
        path=path,
        ontology=ontology,
        require_all_node_types=require_all_node_types,
        external_nodes=external_nodes,
    )


def _fmt_errors(exc: ValidationError, path: str) -> str:
    lines = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    tail = "" if len(exc.errors()) <= 5 else f"\n  …另有 {len(exc.errors()) - 5} 处"
    return f"CMDB 实体文件 schema 校验失败：{path}\n" + "\n".join(lines) + tail


def build_graph(
    data: dict,
    *,
    path: str = "<inline>",
    ontology: Ontology | None = None,
    require_all_node_types: bool = True,
    external_nodes: dict[str, str] | None = None,
) -> EntityGraph:
    """从已解析的 dict 构建图并做完整校验。测试可直接喂 dict（无需落盘）。

    ``ontology`` 供覆盖层（事件文件）复用主图的声明；``require_all_node_types=False``
    允许文件只含部分节点类型（覆盖层场景）。

    ``external_nodes``（id → type）是**主图**的节点表，供覆盖层引用：覆盖层的边可以
    指向主图里的 App（``incident → app``），故端点校验必须把主图节点也算作"存在"。
    """
    try:
        doc = EntityDocument.model_validate(data)
    except ValidationError as exc:
        raise GraphLoadError(_fmt_errors(exc, path), {"path": path}) from exc

    # ---- schema 版本 ----
    major = doc.schema_version.split(".", 1)[0]
    if major != str(SUPPORTED_SCHEMA_MAJOR):
        raise GraphLoadError(
            f"CMDB 实体文件 schema 主版本不支持：文件为 {doc.schema_version}，"
            f"本服务支持 {SUPPORTED_SCHEMA_MAJOR}.x。主版本变更需要迁移，不能直接加载。",
            {"path": path, "file_version": doc.schema_version},
        )

    onto = doc.ontology or ontology
    if onto is None:
        raise GraphLoadError(
            f"CMDB 实体文件缺少 ontology 块，且未从上下文提供：{path}", {"path": path}
        )

    type_keys = [t.key for t in onto.node_types]
    type_key_set = set(type_keys)
    if len(type_key_set) != len(type_keys):
        raise GraphLoadError(f"ontology.node_types 有重复 key：{path}", {"path": path})

    edge_specs = {e.key: e for e in onto.edge_types}
    key_attr_keys = {a.key for a in onto.key_attributes}
    derived_keys = {m.key for m in onto.derived_metrics}

    overlap = key_attr_keys & derived_keys
    if overlap:
        raise GraphLoadError(
            f"ontology 中同一 key 同时被声明为静态标签与派生指标：{sorted(overlap)}——"
            f"二者必须互斥，否则无法判断它该不该写进静态文件。",
            {"path": path, "keys": sorted(overlap)},
        )

    # ---- ontology 自洽：business 层禁止 app—app 边 ----
    #
    # 这条是「app 与 app 之间不能直接关联，要通过业务域」的**强制点**。
    # 放在**声明层**而不是逐边检查：一旦某个 business 边类型被声明成 app—app，
    # 任何数据都必然违规——与其等坏数据进来再报错，不如让这种声明根本无法通过。
    #
    # 注意这不阻止 app—app 的**运行时依赖**：`calls` 归 runtime 层，语义是观测到的
    # 调用事实，不是业务归属。删掉它会让 get_service_topology（爆炸半径/根因）失去
    # 数据源，所以用分层把两件事分开，而不是一刀切禁掉。
    for edge_spec in onto.edge_types:
        if (
            edge_spec.layer == "business"
            and "app" in edge_spec.from_types
            and "app" in edge_spec.to_types
        ):
            raise GraphLoadError(
                f"ontology 声明了 business 层的 app—app 边 {edge_spec.key!r}——"
                f"这违反「app 之间不能直接关联，要通过业务域（portfolio / domain）」。"
                f"若确有同级直连需求，请把它归入 runtime 层（参照 calls 的语义："
                f"观测到的运行时依赖，不是业务归属）。",
                {"path": path, "edge_type": edge_spec.key},
            )

    # ---- 节点类型齐全 ----
    if require_all_node_types:
        missing = type_key_set - set(doc.nodes)
        extra = set(doc.nodes) - type_key_set
        if missing or extra:
            raise GraphLoadError(
                f"nodes 的类型键与 ontology 不一致：缺少 {sorted(missing)}，"
                f"多余 {sorted(extra)}。未录入的类型必须显式写成 []，不能省略键"
                f"——省略会让「没数据」看起来像「这个类型不存在」。",
                {"path": path, "missing": sorted(missing), "extra": sorted(extra)},
            )

    # ---- 逐节点 ----
    nodes: dict[str, dict] = {}
    nodes_by_type: dict[str, list[str]] = {k: [] for k in type_keys}
    by_typed_name: dict[tuple[str, str], str] = {}
    for type_key, items in doc.nodes.items():
        if type_key not in type_key_set:
            raise GraphLoadError(
                f"nodes 中出现 ontology 未声明的节点类型 {type_key!r}：{path}", {"path": path}
            )
        for node in items:
            m = NODE_ID_RE.match(node.id)
            if not m:
                raise GraphLoadError(
                    f"节点 id 格式非法：{node.id!r}（应为 <type>:<slug>，如 app:order-service）",
                    {"path": path, "node_id": node.id},
                )
            if m.group("type") != node.type:
                raise GraphLoadError(
                    f"节点 id 前缀与 type 不符：id={node.id!r} 但 type={node.type!r}",
                    {"path": path, "node_id": node.id},
                )
            if node.type != type_key:
                raise GraphLoadError(
                    f"节点 {node.id!r} 的 type={node.type!r} 与所在分桶 {type_key!r} 不符",
                    {"path": path, "node_id": node.id},
                )
            if node.id in nodes:
                raise GraphLoadError(f"节点 id 重复：{node.id}", {"path": path})
            if not node.name:
                raise GraphLoadError(f"节点 {node.id} 的 name 为空", {"path": path})
            typed = (node.type, node.name)
            if typed in by_typed_name:
                raise GraphLoadError(
                    f"同类型下 name 重复：{node.type}:{node.name}"
                    f"（已由 {by_typed_name[typed]} 占用）",
                    {"path": path},
                )
            by_typed_name[typed] = node.id

            # 属性：已登记的类型严格校验
            attr_model = NODE_ATTR_MODELS.get(node.type)
            if attr_model is not None:
                try:
                    node.attributes = attr_model.model_validate(node.attributes).model_dump()
                except ValidationError as exc:
                    loc = ".".join(str(x) for x in exc.errors()[0]["loc"])
                    raise GraphLoadError(
                        f"节点 {node.id} 的属性不合法：{loc} — {exc.errors()[0]['msg']}",
                        {"path": path, "node_id": node.id},
                    ) from exc

            # 横切标签：静态词表，派生指标硬拒
            for tag in node.tags:
                if tag in derived_keys:
                    raise GraphLoadError(
                        f"{node.id}.tags 含派生指标 {tag!r}——派生指标带时间窗口，"
                        f"会随时间漂移，不能写进静态实体文件；"
                        f"静态标签只能是 {sorted(key_attr_keys)}",
                        {"path": path, "node_id": node.id, "tag": tag},
                    )
                if tag not in key_attr_keys:
                    raise GraphLoadError(
                        f"{node.id}.tags 含未声明的标签 {tag!r}；"
                        f"合法静态标签：{sorted(key_attr_keys)}",
                        {"path": path, "node_id": node.id, "tag": tag},
                    )

            # keywords 是**开放**词表（不像 tags 有枚举），但空串与重复项仍要挡：
            # 它们只会稀释召回，不会带来任何信息。
            blanks = [k for k in node.keywords if not k.strip()]
            if blanks:
                raise GraphLoadError(
                    f"{node.id}.keywords 含空串（{len(blanks)} 个）——空串能匹配任何文本，"
                    f"会把该节点灌进所有查询结果",
                    {"path": path, "node_id": node.id},
                )
            if len(set(node.keywords)) != len(node.keywords):
                dupes = sorted({k for k in node.keywords if node.keywords.count(k) > 1})
                raise GraphLoadError(
                    f"{node.id}.keywords 有重复项：{dupes}",
                    {"path": path, "node_id": node.id, "duplicates": dupes},
                )
            # 同一个词不该既在封闭标签又在开放关键词里——职责混淆会让"该按标签筛还是
            # 按关键词搜"变得说不清。发现即报，让人明确选一边。
            both = sorted(set(node.keywords) & key_attr_keys)
            if both:
                raise GraphLoadError(
                    f"{node.id} 的 keywords 与其封闭标签重名：{both}——"
                    f"请二选一：要能被筛选器穷举就放 tags，只为被文本命中才放 keywords",
                    {"path": path, "node_id": node.id, "overlap": both},
                )

            raw = node.model_dump()
            nodes[node.id] = raw
            nodes_by_type[node.type].append(node.id)

    # ---- refs 必须指向存在的节点（放节点循环之后：ref 可以指向文档里靠后的节点）----
    external_ids = set(external_nodes or {})
    for nid, node_dict in nodes.items():
        for ref_key, target in node_dict["refs"].items():
            if target not in nodes and target not in external_ids:
                raise GraphLoadError(
                    f"节点 {nid} 的 refs.{ref_key}={target!r} 指向不存在的节点",
                    {"path": path, "node_id": nid, "ref": ref_key},
                )

    # ---- 逐边 ----
    def _type_of(nid: str) -> str | None:
        """端点类型：本文件优先，其次主图（覆盖层可引用主图节点）。"""
        if nid in nodes:
            return nodes[nid]["type"]
        return (external_nodes or {}).get(nid)

    seen_edge_ids: set[str] = set()
    seen_triples: set[tuple[str, str, str]] = set()
    edges: list[dict] = []
    for edge in doc.edges:
        if edge.id in seen_edge_ids:
            raise GraphLoadError(f"边 id 重复：{edge.id}", {"path": path})
        seen_edge_ids.add(edge.id)
        spec = edge_specs.get(edge.type)
        if spec is None:
            raise GraphLoadError(
                f"边 {edge.id} 的类型 {edge.type!r} 未在 ontology 声明；"
                f"合法类型：{sorted(edge_specs)}",
                {"path": path, "edge_id": edge.id},
            )
        for end_name, nid in (("from", edge.from_), ("to", edge.to)):
            if _type_of(nid) is None:
                raise GraphLoadError(
                    f"边 {edge.id} 的 {end_name}={nid!r} 指向不存在的节点"
                    f"（既不在本文件，也不在主图中）",
                    {"path": path, "edge_id": edge.id},
                )
        ft, tt = _type_of(edge.from_), _type_of(edge.to)
        allowed = (ft in spec.from_types and tt in spec.to_types) or (
            not spec.directed and ft in spec.to_types and tt in spec.from_types
        )
        if not allowed:
            raise GraphLoadError(
                f"边 {edge.id} 端点类型不被 {edge.type} 允许：{ft} → {tt}；"
                f"该边类型允许 {spec.from_types} → {spec.to_types}"
                f"{'（无向，可反向）' if not spec.directed else ''}",
                {"path": path, "edge_id": edge.id},
            )
        for attr_key, value in edge.attributes.items():
            vocab = spec.attributes.get(attr_key)
            if vocab is None:
                raise GraphLoadError(
                    f"边 {edge.id} 含 {edge.type} 未声明的属性 {attr_key!r}；"
                    f"允许：{sorted(spec.attributes) or '（无）'}",
                    {"path": path, "edge_id": edge.id},
                )
            if value not in vocab:
                raise GraphLoadError(
                    f"边 {edge.id} 的属性 {attr_key}={value!r} 不在词表内：{vocab}",
                    {"path": path, "edge_id": edge.id},
                )
        triple = (edge.type, edge.from_, edge.to)
        if triple in seen_triples:
            raise GraphLoadError(
                f"重复的边（同类型、同起止）：{edge.type} {edge.from_} → {edge.to}",
                {"path": path, "edge_id": edge.id},
            )
        seen_triples.add(triple)
        edges.append(edge.model_dump(by_alias=True))

    graph = _build_indexes(
        path=path,
        doc=doc,
        onto=onto,
        nodes=nodes,
        nodes_by_type=nodes_by_type,
        edges=edges,
        key_attr_keys=frozenset(key_attr_keys),
        derived_keys=frozenset(derived_keys),
    )

    # ---- calls 子图必须只引用已知 app（``cmdb._bfs`` 依赖这个不变量）----
    for caller, callees in graph.depends_on.items():
        for callee, _rel in callees:
            if callee not in graph.services:
                raise GraphLoadError(
                    f"calls 边指向未知应用 {callee!r}（起点 {caller!r}）", {"path": path}
                )
    return graph


def _derive_indexes(
    nodes: dict[str, dict],
    nodes_by_type: dict[str, list[str]],
    edges: list[dict],
) -> tuple[dict[str, dict], dict[str, list[tuple[str, str]]], dict[str, str]]:
    """派生 (services, depends_on, repo_by_app)——与历史 ``_SERVICES`` / ``_DEPENDS_ON`` 同形。

    ``repo`` 自 v5.7 起由 **`app_codebase` 边**派生（原节点字段 ``refs.repo_ref`` 已提升为边）。
    对外仍表现为 ``services[name]["repo"]``，故 ``locate_repo`` 的契约不变。
    """
    # 先收集 app → codebase 的映射（方向不敏感：无向边两个方向都可能写）
    repo_by_app: dict[str, str] = {}
    for edge in edges:
        if edge["type"] != "app_codebase":
            continue
        a, b = edge["from"], edge["to"]
        if nodes[a]["type"] == "app" and nodes[b]["type"] == "codebase":
            repo_by_app[nodes[a]["name"]] = nodes[b]["name"]
        elif nodes[b]["type"] == "app" and nodes[a]["type"] == "codebase":
            repo_by_app[nodes[b]["name"]] = nodes[a]["name"]

    services: dict[str, dict] = {}
    depends_on: dict[str, list[tuple[str, str]]] = {}

    # 按**节点出现顺序**建立 app 索引——保持与历史一致的迭代顺序
    for nid in nodes_by_type.get("app", []):
        node = nodes[nid]
        name = node["name"]
        services[name] = {**node["attributes"], "repo": repo_by_app.get(name, "")}
        depends_on[name] = []

    for edge in edges:
        if edge["type"] != "calls":
            continue
        caller_node, callee_node = nodes.get(edge["from"]), nodes.get(edge["to"])
        if caller_node is None or callee_node is None:
            # 只在覆盖层场景出现：该文件里的 calls 边引用了主图节点（主图节点不在
            # 本文件的 nodes 里）。此处的索引是局部的、不完整——真正的索引由
            # EntityGraph.merged_with 在并集上重建。
            continue
        depends_on.setdefault(caller_node["name"], []).append(
            (callee_node["name"], edge["attributes"].get("relation", ""))
        )

    return services, depends_on, repo_by_app


def _build_indexes(
    *,
    path: str,
    doc: EntityDocument,
    onto: Ontology,
    nodes: dict[str, dict],
    nodes_by_type: dict[str, list[str]],
    edges: list[dict],
    key_attr_keys: frozenset[str],
    derived_keys: frozenset[str],
) -> EntityGraph:
    services, depends_on, repo_by_app = _derive_indexes(nodes, nodes_by_type, edges)
    return EntityGraph(
        path=path,
        schema_version=doc.schema_version,
        ontology=onto,
        metadata=doc.metadata,
        nodes=nodes,
        edges=edges,
        nodes_by_type=nodes_by_type,
        services=services,
        depends_on=depends_on,
        repo_by_app=repo_by_app,
        key_attribute_keys=key_attr_keys,
        derived_metric_keys=derived_keys,
    )


# ======================================================================
# 事件覆盖层（Incident / Change）
# ======================================================================
def resolve_incidents_path() -> str | None:
    """事件覆盖文件路径。**未配置返回 None**（= 没有事件数据，合法状态）。"""
    return (get_settings().datasource_incidents_path or "").strip() or None


@lru_cache(maxsize=8)
def _load_overlay_cached(path: str, mtime_ns: int, base_node_ids: tuple[str, ...]) -> EntityGraph:
    base = get_graph()
    return load_entity_graph(
        path,
        ontology=base.ontology,
        require_all_node_types=False,
        external_nodes={nid: base.nodes[nid]["type"] for nid in base_node_ids},
    )


def load_incidents_overlay() -> EntityGraph | None:
    """读事件覆盖层；**未配置或没数据时返回 None，不报错**。

    这条与主文件的 fail-closed（``get_graph`` 缺文件即抛）**刻意相反**，看着像不一致，
    实则是两种不同的东西：

    - 主文件缺失 = 配置错了。那时给空图会把"文件没挂上"洗成"这服务没有依赖"。
    - 事件文件缺失 = 还没数据。这是**合法且常见**的状态（本系统当前就是），
      不是错误；因此走 `degraded` 标记而不是异常。

    但**配置了路径而文件不存在**是另一回事——那是路径写错了，属于配置错误，照抛。
    """
    path = resolve_incidents_path()
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise GraphLoadError(
            f"已配置 DATASOURCE_INCIDENTS_PATH 但文件不可读：{path}（{exc.strerror}）。"
            f"若暂无事件数据，请把该配置**留空**——留空表示「没有事件数据」，是合法状态。",
            {"path": path},
        ) from exc
    base = get_graph()
    return _load_overlay_cached(path, stat.st_mtime_ns, tuple(sorted(base.nodes)))


def clear_overlay_cache() -> None:
    _load_overlay_cached.cache_clear()


def get_effective_graph() -> EntityGraph:
    """主图 + 事件覆盖层（若有）。工具取数一律走这个。"""
    base = get_graph()
    overlay = load_incidents_overlay()
    return base.merged_with(overlay) if overlay is not None else base


__all__ = [
    "EntityGraph",
    "EntityDocument",
    "GraphLoadError",
    "Ontology",
    "SUPPORTED_SCHEMA_MAJOR",
    "build_graph",
    "clear_graph_cache",
    "clear_overlay_cache",
    "get_effective_graph",
    "get_graph",
    "load_entity_graph",
    "load_incidents_overlay",
    "resolve_cmdb_path",
    "resolve_incidents_path",
]
