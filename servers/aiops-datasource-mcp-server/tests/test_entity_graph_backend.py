"""实体图谱加载器：校验器逐条生效（失败矩阵）。

校验器的价值全在**能不能挡住坏数据**。这里每条校验都配一个反例——尤其是
「派生指标不许写进静态文件」那条：它是靠校验器强制、而不是靠文档约定。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.backends.entity_graph import GraphLoadError
from aiops_datasource_mcp_server.errors import ErrorCode


@pytest.fixture
def base_doc(baseline_cmdb) -> dict:
    """以**冻结基线**为基准做变异——顺带保证基准本身是合法的。

    ⚠️ 基准用冻结副本而不是包内活文件：后者现在是可编辑的用户数据，会随人工编辑
    变化，取件顺序类的断言（`doc["nodes"]["app"][0]` 是谁）会跟着飘。
    """
    return json.loads(Path(baseline_cmdb).read_text(encoding="utf-8"))


def _write(tmp_path: Path, doc: dict, name: str = "cmdb.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _load(tmp_path: Path, doc: dict) -> eg.EntityGraph:
    return eg.load_entity_graph(_write(tmp_path, doc))


def _app(doc: dict, name: str) -> dict:
    return next(n for n in doc["nodes"]["app"] if n["name"] == name)


# ======================================================================
# 正常路径
# ======================================================================
def _strip_business(doc: dict) -> dict:
    """清掉种子文件里的业务层节点**与它们的边**，便于测试自建受控的业务层。

    ⚠️ 只清节点不清边会留下悬空端点——加载器会（正确地）报错。这个 helper 的存在
    本身就是那条校验在起作用的旁证。
    """
    biz = ("enterprise", "journey", "portfolio", "domain")
    biz_ids = {n["id"] for t in biz for n in doc["nodes"].get(t, [])}
    for t in biz:
        doc["nodes"][t] = []
    doc["edges"] = [
        e for e in doc["edges"] if e["from"] not in biz_ids and e["to"] not in biz_ids
    ]
    return doc


def test_default_file_loads(tmp_path, clear_settings_cache) -> None:
    """包内那份**活文件**加载得动，且结构完整。

    ⚠️ **这里刻意不断言数量。** 活文件是可编辑的用户数据（编辑页 + `/admin/cmdb/**`），
    钉住 `node_count == 102` 这类数字会让**每次人工编辑都触发假失败**——而编辑正是
    这个功能的目的。数量与"历史字段逐字未变"由 `test_cmdb_entities_data.py` 对着
    **冻结基线**（`tests/fixtures/cmdb-migration-baseline.json`）验证。

    这条只保证一件事，而它很重要：**随 wheel 分发的那份文件本身是合法的**。
    它坏掉的话所有 CMDB 工具都会 fail-closed，且只在装了包的环境里才暴露。
    """
    graph = eg.get_graph()
    assert graph.schema_version.startswith("1.")
    assert set(graph.nodes_by_type) == {t.key for t in graph.ontology.node_types}
    assert graph.node_count > 0
    assert len(graph.edges) > 0


def test_explicit_path_load_writes_roundtrip(tmp_path, base_doc) -> None:
    graph = _load(tmp_path, base_doc)
    assert graph.node_count == 102
    assert len(graph.services) == 60


# ======================================================================
# 文件级失败
# ======================================================================
def test_missing_file_fails_closed(clear_settings_cache, monkeypatch) -> None:
    """缺文件 → CONFIG_ERROR（不给空图：空图会把配置错误洗成"这服务没依赖"）。"""
    monkeypatch.setenv("DATASOURCE_CMDB_PATH", "/nope/definitely-missing.json")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(GraphLoadError) as ei:
        eg.get_graph()
    assert ei.value.code is ErrorCode.CONFIG_ERROR
    assert "不可读" in str(ei.value)


def test_bad_json_reports_line_and_column(tmp_path) -> None:
    p = tmp_path / "broken.json"
    p.write_text('{"schema_version": "1.0.0",,}', encoding="utf-8")
    with pytest.raises(GraphLoadError) as ei:
        eg.load_entity_graph(str(p))
    assert "不是合法 JSON" in str(ei.value)
    assert ei.value.details and ei.value.details["line"] >= 1


def test_major_schema_version_mismatch_rejected(tmp_path, base_doc) -> None:
    """主版本不符 = 需迁移，拒绝加载；minor/patch 差异则放行。"""
    doc = copy.deepcopy(base_doc)
    doc["schema_version"] = "2.0.0"
    with pytest.raises(GraphLoadError, match="主版本不支持"):
        _load(tmp_path, doc)

    doc["schema_version"] = "1.7.3"  # minor/patch 前进兼容
    assert _load(tmp_path, doc).schema_version == "1.7.3"


# ======================================================================
# 结构失败
# ======================================================================
def test_missing_node_type_key_rejected(tmp_path, base_doc) -> None:
    """省略键会让「没数据」看起来像「这个类型不存在」——必须显式 []。"""
    doc = copy.deepcopy(base_doc)
    del doc["nodes"]["journey"]
    with pytest.raises(GraphLoadError, match="类型键与 ontology 不一致"):
        _load(tmp_path, doc)


def test_undeclared_node_type_key_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    doc["nodes"]["datacenter"] = []
    with pytest.raises(GraphLoadError, match="类型键与 ontology 不一致"):
        _load(tmp_path, doc)


def test_unknown_top_level_key_rejected(tmp_path, base_doc) -> None:
    """顶层 extra="forbid"——打错的键不会静默被忽略。"""
    doc = copy.deepcopy(base_doc)
    doc["edegs"] = []
    with pytest.raises(GraphLoadError, match="schema 校验失败"):
        _load(tmp_path, doc)


# ======================================================================
# 节点失败
# ======================================================================
def test_node_id_prefix_must_match_type(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["id"] = "service:order-service"
    with pytest.raises(GraphLoadError, match="前缀与 type 不符"):
        _load(tmp_path, doc)


def test_duplicate_node_id_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    dup = copy.deepcopy(_app(doc, "order-service"))
    doc["nodes"]["app"].append(dup)
    with pytest.raises(GraphLoadError, match="节点 id 重复"):
        _load(tmp_path, doc)


def test_duplicate_name_within_type_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    dup = copy.deepcopy(_app(doc, "order-service"))
    dup["id"] = "app:order-service-2"
    doc["nodes"]["app"].append(dup)
    with pytest.raises(GraphLoadError, match="name 重复"):
        _load(tmp_path, doc)


def test_app_attributes_validated(tmp_path, base_doc) -> None:
    """app 属性走严格模型：tier / criticality 是枚举，多余属性也拒。"""
    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["attributes"]["criticality"] = "super-critical"
    with pytest.raises(GraphLoadError, match="属性不合法"):
        _load(tmp_path, doc)

    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["attributes"]["squad"] = "新加的"
    with pytest.raises(GraphLoadError, match="属性不合法"):
        _load(tmp_path, doc)


# ======================================================================
# 横切标签：静态 vs 派生（靠校验器强制，不靠约定）
# ======================================================================
def test_derived_metric_as_tag_rejected(tmp_path, base_doc) -> None:
    """派生指标带时间窗口、会漂移，绝不能写进静态实体文件。"""
    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["tags"] = ["top10_incidents_18m"]
    with pytest.raises(GraphLoadError) as ei:
        _load(tmp_path, doc)
    msg = str(ei.value)
    assert "派生指标" in msg and "静态标签" in msg


def test_undeclared_tag_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["tags"] = ["critical-app"]  # 同义但未声明
    with pytest.raises(GraphLoadError, match="未声明的标签"):
        _load(tmp_path, doc)


def test_key_attribute_may_not_also_be_derived(tmp_path, base_doc) -> None:
    """同一 key 不能既是静态标签又是派生指标——否则无法判断该不该落盘。"""
    doc = copy.deepcopy(base_doc)
    doc["ontology"]["key_attributes"].append(
        {"key": "top10_incidents_18m", "label": "X", "label_zh": "X"}
    )
    with pytest.raises(GraphLoadError, match="必须互斥"):
        _load(tmp_path, doc)


def test_static_tag_is_accepted(tmp_path, base_doc) -> None:
    """正向对照：合法静态标签放行。"""
    doc = copy.deepcopy(base_doc)
    _app(doc, "audit-service")["tags"] = ["finance_freeze"]
    graph = _load(tmp_path, doc)
    assert graph.nodes["app:audit-service"]["tags"] == ["finance_freeze"]


# ======================================================================
# 引用与边失败
# ======================================================================
def test_dangling_repo_ref_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    _app(doc, "order-service")["refs"]["repo_ref"] = "codebase:no-such-repo"
    with pytest.raises(GraphLoadError, match="指向不存在的节点"):
        _load(tmp_path, doc)


def test_dangling_edge_endpoint_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    doc["edges"][0]["to"] = "app:no-such-service"
    with pytest.raises(GraphLoadError, match="指向不存在的节点"):
        _load(tmp_path, doc)


def test_unknown_edge_type_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    doc["edges"][0]["type"] = "depends_on"
    with pytest.raises(GraphLoadError, match="未在 ontology 声明"):
        _load(tmp_path, doc)


def test_endpoint_type_mismatch_rejected(tmp_path, base_doc) -> None:
    """app_codebase 只允许 app — codebase；把一端改成 app 成 app—app 必须被拒。"""
    doc = copy.deepcopy(base_doc)
    link = next(e for e in doc["edges"] if e["type"] == "app_codebase")
    link["to"] = "app:payment-service"
    with pytest.raises(GraphLoadError, match="端点类型不被"):
        _load(tmp_path, doc)


def test_undirected_edge_accepts_either_direction(tmp_path, base_doc) -> None:
    """无向边两个方向都合法（app — codebase / codebase — app）。"""
    doc = copy.deepcopy(base_doc)
    link = next(e for e in doc["edges"] if e["type"] == "app_codebase")
    link["id"] = "e:reversed"
    link["from"], link["to"] = link["to"], link["from"]
    assert _load(tmp_path, doc) is not None


# ======================================================================
# 分层约束：business 层禁止 app—app
# ======================================================================
def test_business_layer_app_app_edge_rejected(tmp_path, base_doc) -> None:
    """「app 与 app 之间不能直接关联」由**声明层**强制。

    一旦某个 business 边类型被声明成 app—app，任何数据都必然违规——
    与其等坏数据进来再报错，不如让这种声明根本无法通过。
    """
    doc = copy.deepcopy(base_doc)
    doc["ontology"]["edge_types"].append(
        {
            "key": "app_app_business", "label": "X", "label_zh": "X",
            "directed": False, "from_types": ["app"], "to_types": ["app"],
            "attributes": {}, "origin": "local", "layer": "business",
        }
    )
    with pytest.raises(GraphLoadError, match="business 层的 app—app 边"):
        _load(tmp_path, doc)


def test_runtime_layer_app_app_edge_is_fine(tmp_path, base_doc) -> None:
    """正向对照：`calls`（app—app）归 runtime 层，必须放行。

    它是 get_service_topology（爆炸半径/根因）的唯一数据源——一刀切禁掉 app—app
    会连带摧毁这个能力。分层把"业务归属"与"运行时依赖"分开。
    """
    graph = _load(tmp_path, base_doc)
    calls = [e for e in graph.edges if e["type"] == "calls"]
    assert len(calls) == 13


def test_edge_type_requires_layer(tmp_path, base_doc) -> None:
    """边类型必须声明 layer——没有它就无法执行分层约束。"""
    doc = copy.deepcopy(base_doc)
    for spec in doc["ontology"]["edge_types"]:
        if spec["key"] == "calls":
            del spec["layer"]
    with pytest.raises(GraphLoadError, match="schema 校验失败"):
        _load(tmp_path, doc)


def test_domain_is_a_declared_node_type(tmp_path, base_doc) -> None:
    """v5.7 新增 domain（业务细域），与 portfolio 平行、直连 app。"""
    graph = _load(tmp_path, base_doc)
    assert "domain" in graph.nodes_by_type
    spec = next(e for e in graph.ontology.edge_types if e.key == "domain_link")
    assert spec.layer == "business"
    assert spec.from_types == ["domain"] and spec.to_types == ["app"]


def test_business_domain_attributes_accepted(tmp_path, base_doc) -> None:
    """业务层节点：`description` / `keywords` 在**信封层**，`capability` 在 attributes。

    描述与检索词是所有节点类型共有的（它们与 `display_name` 同类），所以放信封层；
    attributes 只留业务层独有的概念——避免每个类型各定义一个同名同义的字段。
    """
    doc = _strip_business(copy.deepcopy(base_doc))
    doc["nodes"]["domain"] = [
        {
            "id": "domain:ticket-print", "type": "domain", "name": "ticket-print",
            "display_name": "工单打印",
            "description": "打印工单并跟踪打印结果",
            "keywords": ["打印工单", "打印没反应"],
            "attributes": {"capability": "Ticket Printing"},
            "tags": [], "refs": {},
        }
    ]
    doc["edges"].append(
        {
            "id": "e:domain-print", "type": "domain_link",
            "from": "domain:ticket-print", "to": "app:order-service", "attributes": {},
        }
    )
    graph = _load(tmp_path, doc)
    node = graph.nodes["domain:ticket-print"]
    assert node["keywords"] == ["打印工单", "打印没反应"]
    assert node["description"] == "打印工单并跟踪打印结果"
    assert node["attributes"]["capability"] == "Ticket Printing"


def test_business_domain_attributes_reject_unknown_key(tmp_path, base_doc) -> None:
    """业务层 attributes 是严格模型——打错的键不会静默忽略。

    `terms` 是 v5.7 撤掉的字段名（已统一到信封层的 `keywords`），
    所以它现在**必须**被拒——否则写了它的人会以为检索词生效了，实际是死的。
    """
    doc = _strip_business(copy.deepcopy(base_doc))
    doc["nodes"]["domain"] = [
        {
            "id": "domain:x", "type": "domain", "name": "x",
            "attributes": {"terms": ["已废弃的字段名"]},
            "tags": [], "refs": {},
        }
    ]
    with pytest.raises(GraphLoadError, match="属性不合法"):
        _load(tmp_path, doc)


# ======================================================================
# description / keywords（所有节点类型共有）
# ======================================================================
def test_description_and_keywords_are_on_every_node(tmp_path, base_doc) -> None:
    """v5.7 补齐：每个节点都有描述与检索词——这是「问题 → 服务」能匹配的前提。"""
    graph = _load(tmp_path, base_doc)
    for nid, node in graph.nodes.items():
        assert node["description"], f"{nid} 缺 description"
        assert node["keywords"], f"{nid} 缺 keywords"


def test_keywords_blank_string_rejected(tmp_path, base_doc) -> None:
    """空串能匹配任何文本——会把该节点灌进所有查询结果。"""
    doc = copy.deepcopy(base_doc)
    doc["nodes"]["app"][0]["keywords"] = ["网关", "  "]
    with pytest.raises(GraphLoadError, match="含空串"):
        _load(tmp_path, doc)


def test_keywords_duplicate_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    doc["nodes"]["app"][0]["keywords"] = ["网关", "网关"]
    with pytest.raises(GraphLoadError, match="有重复项"):
        _load(tmp_path, doc)


def test_keywords_may_not_shadow_closed_tags(tmp_path, base_doc) -> None:
    """同一个词不该既在封闭标签又在开放关键词里。

    职责混淆会让「该按标签筛还是按关键词搜」说不清——发现即报，逼人明确选一边。
    """
    doc = copy.deepcopy(base_doc)
    doc["nodes"]["app"][1]["keywords"] = ["订单", "tier1"]   # order-service 本就带 tier1
    with pytest.raises(GraphLoadError, match="与其封闭标签重名"):
        _load(tmp_path, doc)


def test_keywords_are_open_vocabulary(tmp_path, base_doc) -> None:
    """正向对照：keywords 是**开放**词表（不像 tags 有枚举），任意词都收。

    按 **name 取节点**而不是按下标：``nodes["app"]`` 的顺序取决于来源文件里应用的
    排列，OTR 整合后第 0 个已经不是 gateway-service 了。用下标会得到一条
    "改的是 A、断言的是 B"的测试——它照样会红，但红得莫名其妙。
    """
    doc = copy.deepcopy(base_doc)
    _app(doc, "gateway-service")["keywords"] = ["任意新词", "尚未声明的说法"]
    graph = _load(tmp_path, doc)
    assert graph.nodes["app:gateway-service"]["keywords"] == ["任意新词", "尚未声明的说法"]


def test_edge_attribute_outside_vocabulary_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    call = next(e for e in doc["edges"] if e["type"] == "calls")
    call["attributes"]["relation"] = "grpc"  # 词表只有 http/rpc/mq
    with pytest.raises(GraphLoadError, match="不在词表内"):
        _load(tmp_path, doc)


def test_undeclared_edge_attribute_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    call = next(e for e in doc["edges"] if e["type"] == "calls")
    call["attributes"]["timeout_ms"] = 500
    with pytest.raises(GraphLoadError, match="未声明的属性"):
        _load(tmp_path, doc)


def test_duplicate_edge_triple_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    call = next(e for e in doc["edges"] if e["type"] == "calls")
    dup = copy.deepcopy(call)
    dup["id"] = "e:duplicate-relation"
    doc["edges"].append(dup)
    with pytest.raises(GraphLoadError, match="重复的边"):
        _load(tmp_path, doc)


def test_duplicate_edge_id_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    call = next(e for e in doc["edges"] if e["type"] == "calls")
    dup = copy.deepcopy(call)
    dup["to"] = "app:user-service"
    doc["edges"].append(dup)
    with pytest.raises(GraphLoadError, match="边 id 重复"):
        _load(tmp_path, doc)


# ======================================================================
# 覆盖层模式
# ======================================================================
def test_overlay_may_omit_node_types(tmp_path, base_doc) -> None:
    """事件覆盖层只需 incident 类型，其余键可省——复用主图 ontology。"""
    base = eg.get_graph()
    overlay = {
        "schema_version": "1.0.0",
        "nodes": {
            "incident": [
                {
                    "id": "incident:INC-1", "type": "incident", "name": "INC-1",
                    "attributes": {}, "tags": [], "refs": {},
                }
            ]
        },
        "edges": [
            {
                "id": "e:incident-1", "type": "incident_cluster_app",
                "from": "incident:INC-1", "to": "app:order-service", "attributes": {},
            }
        ],
    }
    path = _write(tmp_path, overlay, name="incidents.json")
    graph = eg.load_entity_graph(
        path,
        ontology=base.ontology,
        require_all_node_types=False,
        external_nodes={nid: n["type"] for nid, n in base.nodes.items()},
    )
    assert graph.nodes_by_type["incident"] == ["incident:INC-1"]
    assert graph.edges[0]["to"] == "app:order-service"


def test_overlay_without_ontology_rejected(tmp_path, base_doc) -> None:
    doc = copy.deepcopy(base_doc)
    del doc["ontology"]
    with pytest.raises(GraphLoadError, match="缺少 ontology"):
        _load(tmp_path, doc)
