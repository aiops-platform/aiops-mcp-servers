"""事件覆盖层：主图 + Incident/Change 的只读合并。

覆盖层的存在意义是把「问题 → 事件 → 应用」这条路径打通，**且不需要改代码**——
没有事件数据时整段跳过，数据到位那天它自己就通了。所以这里既测"有数据时能走通"，
也测"没数据时不是错误"。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.backends import graph_query as gq
from aiops_datasource_mcp_server.backends.entity_graph import GraphLoadError
from aiops_datasource_mcp_server.config import get_settings

OVERLAY: dict = {
    "schema_version": "1.0.0",
    "nodes": {
        "incident": [
            {
                "id": "incident:INC-1001", "type": "incident", "name": "INC-1001",
                "display_name": "订单超时告警", "attributes": {"severity": "P1"},
                "tags": [], "refs": {}, "notes": "checkout latency storm",
            }
        ],
        "change": [
            {
                "id": "change:CHG-77", "type": "change", "name": "CHG-77",
                "display_name": None, "attributes": {"kind": "emergency"},
                "tags": [], "refs": {}, "notes": None,
            }
        ],
    },
    "edges": [
        {
            "id": "e:inc-1001-audit", "type": "incident_cluster_app",
            "from": "incident:INC-1001", "to": "app:audit-service", "attributes": {},
        },
        {
            "id": "e:chg-77-audit", "type": "change_cluster_app",
            "from": "change:CHG-77", "to": "app:payment-service", "attributes": {},
        },
    ],
}


@pytest.fixture
def overlay_path(tmp_path: Path) -> str:
    p = tmp_path / "incidents.json"
    p.write_text(json.dumps(OVERLAY, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _use_overlay(monkeypatch, path: str) -> None:
    monkeypatch.setenv("DATASOURCE_INCIDENTS_PATH", path)
    get_settings.cache_clear()
    eg.clear_overlay_cache()


# ======================================================================
# 未配置 / 配置错
# ======================================================================
def test_unset_overlay_is_not_an_error(clear_settings_cache) -> None:
    """**未配置 = 没有事件数据 = 合法状态**（与主文件 fail-closed 刻意相反）。"""
    assert eg.resolve_incidents_path() is None
    assert eg.load_incidents_overlay() is None
    assert eg.get_effective_graph() is eg.get_graph()


def test_configured_but_missing_file_is_a_config_error(monkeypatch, clear_settings_cache) -> None:
    """配置了路径却读不到 = 路径写错了，属于配置错误——与"没数据"必须区别对待。"""
    monkeypatch.setenv("DATASOURCE_INCIDENTS_PATH", "/nope/incidents.json")
    get_settings.cache_clear()
    with pytest.raises(GraphLoadError) as ei:
        eg.load_incidents_overlay()
    assert "留空" in str(ei.value)


# ======================================================================
# 合并
# ======================================================================
def test_overlay_merges_into_base(monkeypatch, clear_settings_cache, overlay_path) -> None:
    _use_overlay(monkeypatch, overlay_path)
    merged = eg.get_effective_graph()
    assert merged.node_count == eg.get_graph().node_count + 2
    assert "incident:INC-1001" in merged.nodes
    assert "change:CHG-77" in merged.nodes
    assert len(merged.edges) == len(eg.get_graph().edges) + 2


def test_merge_does_not_mutate_base(monkeypatch, clear_settings_cache, overlay_path) -> None:
    base = eg.get_graph()
    before_nodes, before_edges = base.node_count, len(base.edges)
    _use_overlay(monkeypatch, overlay_path)
    eg.get_effective_graph()
    assert base.node_count == before_nodes
    assert len(base.edges) == before_edges


def test_overlay_may_reuse_base_node_types(monkeypatch, clear_settings_cache, overlay_path) -> None:
    """覆盖层缺的类型键由合并补齐——主图的 12 类仍然齐全。"""
    _use_overlay(monkeypatch, overlay_path)
    merged = eg.get_effective_graph()
    for type_key in ("app", "journey", "wiki", "tool"):
        assert type_key in merged.nodes_by_type


def test_node_id_collision_rejected() -> None:
    """覆盖层只应**新增**事件节点，重定义主图节点必须报错。"""
    base = eg.get_graph()
    doc = copy.deepcopy(OVERLAY)
    # 覆盖层里出现一个与主图同 id 的 app —— 即"重定义主图节点"
    doc["nodes"]["app"] = [copy.deepcopy(base.nodes["app:order-service"])]
    overlay = eg.build_graph(
        doc, ontology=base.ontology, require_all_node_types=False,
        external_nodes={nid: n["type"] for nid, n in base.nodes.items()},
    )
    with pytest.raises(GraphLoadError, match="重复节点 id"):
        base.merged_with(overlay)


def test_duplicate_edge_rejected() -> None:
    """覆盖层重复主图已有的边 → 合并时报错，而不是静默去重。"""
    base = eg.get_graph()
    doc = copy.deepcopy(OVERLAY)
    # 覆盖层自己新增一条与主图完全相同的 calls 边（端点用主图节点）
    doc["edges"].append(
        {
            "id": "e:overlay-dup",
            "type": "calls",
            "from": "app:gateway-service",
            "to": "app:order-service",
            "attributes": {"relation": "http"},
        }
    )
    overlay = eg.build_graph(
        doc, ontology=base.ontology, require_all_node_types=False,
        external_nodes={nid: n["type"] for nid, n in base.nodes.items()},
    )
    with pytest.raises(GraphLoadError, match="重复的边"):
        base.merged_with(overlay)


def test_overlay_edge_to_unknown_base_node_rejected() -> None:
    base = eg.get_graph()
    doc = copy.deepcopy(OVERLAY)
    doc["edges"][0]["to"] = "app:no-such-app"
    with pytest.raises(GraphLoadError, match="指向不存在的节点"):
        eg.build_graph(
            doc, ontology=base.ontology, require_all_node_types=False,
            external_nodes={nid: n["type"] for nid, n in base.nodes.items()},
        )


# ======================================================================
# 端到端：事件路径真的参与推断
# ======================================================================
def test_incident_match_reaches_app(monkeypatch, clear_settings_cache, overlay_path) -> None:
    """问题描述对上事件节点 → 沿事件→应用边落到 App。

    注意 `audit-service` 与 `payment-service` 都不是 `warranty-service` 的依赖邻居，
    所以它出现在候选里**只能**来自事件路径。
    """
    _use_overlay(monkeypatch, overlay_path)
    merged = eg.get_effective_graph()
    r = gq.infer_candidates(merged, problem="订单超时告警", services=["warranty-service"])

    services = {c["service"] for c in r["candidate_apps"]}
    assert "audit-service" in services
    assert "incident_match" in r["evidence_used"]
    assert r["incident_data_available"] is True
    assert r["degraded"] is False
    assert "已纳入 Incident" in r["coverage_note"]


def test_change_match_reaches_app(monkeypatch, clear_settings_cache, overlay_path) -> None:
    _use_overlay(monkeypatch, overlay_path)
    merged = eg.get_effective_graph()
    r = gq.infer_candidates(merged, problem="emergency 变更引发的问题")
    assert "payment-service" in {c["service"] for c in r["candidate_apps"]}


def test_no_data_keeps_degraded_flag(clear_settings_cache) -> None:
    """没有覆盖层时如实降级——不是错误，是当前状态。"""
    r = gq.infer_candidates(eg.get_effective_graph(), problem="x", services=["warranty-service"])
    assert r["incident_data_available"] is False
    assert r["degraded"] is True
    assert "incident_match" not in r["evidence_used"]
