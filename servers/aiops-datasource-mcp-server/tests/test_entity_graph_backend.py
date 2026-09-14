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
def base_doc() -> dict:
    """以**真实实体文件**为基准做变异——顺带保证基准本身是合法的。"""
    return json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))


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
def test_default_file_loads(tmp_path, clear_settings_cache) -> None:
    graph = eg.get_graph()
    assert graph.schema_version == "1.0.0"
    assert graph.node_count == 26          # 10 app + 10 codebase + 6 portfolio
    assert len(graph.edges) == 23          # 13 calls + 10 portfolio_link


def test_explicit_path_load_writes_roundtrip(tmp_path, base_doc) -> None:
    graph = _load(tmp_path, base_doc)
    assert graph.node_count == 26
    assert len(graph.services) == 10


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
    """portfolio_link 只允许 portfolio — app；app — app 必须被拒。"""
    doc = copy.deepcopy(base_doc)
    link = next(e for e in doc["edges"] if e["type"] == "portfolio_link")
    link["from"], link["to"] = "app:order-service", "app:payment-service"
    with pytest.raises(GraphLoadError, match="端点类型不被"):
        _load(tmp_path, doc)


def test_undirected_edge_accepts_either_direction(tmp_path, base_doc) -> None:
    """无向边两个方向都合法（portfolio — app / app — portfolio）。"""
    doc = copy.deepcopy(base_doc)
    link = next(e for e in doc["edges"] if e["type"] == "portfolio_link")
    link["id"] = "e:reversed"
    link["from"], link["to"] = link["to"], link["from"]
    assert _load(tmp_path, doc) is not None


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
