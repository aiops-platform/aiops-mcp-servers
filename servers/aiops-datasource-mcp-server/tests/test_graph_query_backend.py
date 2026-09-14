"""图查询：四个维度的筛选、facets 现算、未知值 fail-closed、聚焦展开。"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.backends import graph_query as gq
from aiops_datasource_mcp_server.errors import AppError, ErrorCode


@pytest.fixture
def graph(clear_settings_cache) -> eg.EntityGraph:
    return eg.get_graph()


def _ids(result: dict) -> set[str]:
    return {n["id"] for n in result["nodes"]}


# ======================================================================
# 基线
# ======================================================================
def test_no_filter_returns_whole_graph(graph) -> None:
    r = gq.query_graph(graph)
    assert r["matched"]["nodes"] == 26          # 10 app + 10 codebase + 6 portfolio
    assert r["matched"]["edges"] == 23          # 13 calls + 10 portfolio_link
    assert r["counts"]["node_types"]["app"] == 10
    assert r["counts"]["node_types"]["journey"] == 0
    assert r["counts"]["edge_types"]["calls"] == 13
    assert r["counts"]["edge_types"]["support"] == 0


def test_facets_cover_all_four_dimensions(graph) -> None:
    r = gq.query_graph(graph)
    f = r["facets"]
    assert len(f["node_types"]) == 12
    assert len(f["edge_types"]) == 11
    assert len(f["key_attributes"]) == 4
    assert {p["key"] for p in f["portfolios"]} == {
        "order", "payment", "inventory", "logistics", "common", "account"
    }
    # 计数是现算的，不是存储的
    assert {p["key"]: p["count"] for p in f["portfolios"]}["order"] == 4
    assert {a["key"]: a["count"] for a in f["key_attributes"]}["tier1"] == 2
    assert {a["key"]: a["count"] for a in f["key_attributes"]}["holiday_critical"] == 0


def test_derived_metrics_are_declared_but_unavailable(graph) -> None:
    """派生指标只声明、不可用——它们带时间窗口，不进静态文件。"""
    r = gq.query_graph(graph)
    assert len(r["derived_metrics_declared"]) == 3
    assert all(d["available"] is False for d in r["derived_metrics_declared"])
    assert {d["key"] for d in r["derived_metrics_declared"]} == {
        "top10_incidents_18m", "top10_changes_13m", "emergency_changes"
    }


# ======================================================================
# 四个维度
# ======================================================================
def test_node_type_dimension(graph) -> None:
    r = gq.query_graph(graph, node_types=["app"])
    assert r["matched"]["nodes"] == 10
    assert all(n["type"] == "app" for n in r["nodes"])
    assert r["matched"]["edges"] == 13   # 只剩 calls


def test_portfolio_dimension(graph) -> None:
    """选一个业务域 → 该域下的应用 + 域节点本身。"""
    r = gq.query_graph(graph, portfolios=["order"])
    assert _ids(r) == {
        "portfolio:order", "app:gateway-service", "app:order-service",
        "app:warranty-service", "app:pricing-service",
    }
    assert "app:payment-service" not in _ids(r)


def test_key_attribute_dimension(graph) -> None:
    r = gq.query_graph(graph, key_attributes=["tier1"])
    assert _ids(r) == {"app:order-service", "app:payment-service"}


def test_declared_but_unused_key_attribute_returns_empty_not_error(graph) -> None:
    """`holiday_critical` 是**声明过的**合法取值——过滤它返回空是正常语义，不报错。

    这与"传了不存在的取值"必须区分开：前者是数据尚未录入，后者是拼写错误。
    """
    r = gq.query_graph(graph, key_attributes=["holiday_critical"])
    assert r["matched"]["nodes"] == 0
    assert r["summary"].startswith("命中 0 个节点")


def test_edge_type_dimension(graph) -> None:
    r = gq.query_graph(graph, edge_types=["portfolio_link"])
    assert r["matched"]["edges"] == 10
    assert all(e["type"] == "portfolio_link" for e in r["edges"])


def test_dimensions_compose(graph) -> None:
    """维度可叠加：order 业务域 ∩ 带 tier1 标签。"""
    r = gq.query_graph(graph, portfolios=["order"], key_attributes=["tier1"])
    assert _ids(r) == {"app:order-service"}


# ======================================================================
# 未知值 fail-closed
# ======================================================================
def test_unknown_node_type_lists_valid_values(graph) -> None:
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, node_types=["nope"])
    assert ei.value.code is ErrorCode.INVALID_REQUEST
    assert "valid" in (ei.value.details or {})
    assert "app" in ei.value.details["valid"]


def test_unknown_edge_type_lists_valid_values(graph) -> None:
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, edge_types=["depends_on"])
    assert "calls" in ei.value.details["valid"]


def test_unknown_portfolio_explains_it_is_not_a_typo(graph) -> None:
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, portfolios=["Retail"])
    assert "不是拼写问题" not in str(ei.value)   # 有数据时走的是"列合法值"分支
    assert "order" in ei.value.details["valid"]


def test_unknown_key_attribute_rejected(graph) -> None:
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, key_attributes=["top10_incidents_18m"])
    assert "tier1" in ei.value.details["valid"]


def test_empty_vocabulary_says_not_a_typo(tmp_path, graph) -> None:
    """维度整维为空时报"数据未录入"，避免 agent 无限换拼写重试。"""
    doc = {
        "schema_version": "1.0.0",
        "ontology": {
            "node_types": [
                {"key": "journey", "label": "Journey", "label_zh": "旅程", "view": "business"}
            ],
            "edge_types": [],
            "key_attributes": [],
            "derived_metrics": [],
        },
        "nodes": {"journey": [
            {"id": "journey:x", "type": "journey", "name": "x", "attributes": {},
             "tags": [], "refs": {}}
        ]},
        "edges": [],
    }
    empty = eg.build_graph(doc)
    with pytest.raises(AppError, match="不是拼写问题"):
        gq.query_graph(empty, portfolios=["anything"])


# ======================================================================
# 聚焦展开
# ======================================================================
def test_focus_expands_neighbourhood(graph) -> None:
    r = gq.query_graph(graph, node_id="app:order-service", hops=1)
    ids = _ids(r)
    assert "app:order-service" in ids
    assert "app:gateway-service" in ids      # 上游 1 跳
    assert "app:warranty-service" in ids     # 下游 1 跳
    assert "app:audit-service" not in ids    # 2 跳开外
    assert all(n["distance"] is not None for n in r["nodes"])


async def test_focus_matches_topology_at_one_hop(graph) -> None:
    """1 跳上两个实现必须给出相同结果——交叉印证。"""
    from aiops_datasource_mcp_server.backends import cmdb

    topo = await cmdb.get_service_topology("order-service", 1)
    topo_ids = {f"app:{n['service']}" for n in topo["nodes"]}
    q = gq.query_graph(graph, node_id="app:order-service", hops=1, edge_types=["calls"])
    assert topo_ids == _ids(q)


def test_focus_is_superset_of_topology_beyond_one_hop(graph) -> None:
    """2 跳起两者**故意不同**——差异被钉住，免得日后有人"修"成一致。

    区别在**遍历语义**，不在实现对错：

    - `get_service_topology` 的方向是**相对起点**的（见 ``cmdb.py`` 的注释）。从 order
      看，``user-service`` 是"上游的其他下游"即**兄弟节点**，不属于它的上下游。
      这是正确的**诊断**语义——兄弟服务挂了不是 order 的爆炸半径，也不是它的根因。
    - 图查询按**无向邻域**展开。它要跨 11 类边，其中 ``portfolio_link`` / ``journey_link``
      等**根本没有方向**，"上下游"无从谈起；而且它的用途是**探索**（参考界面的
      blast-radius 聚焦），不是诊断判定。

    所以这里断言的是**包含关系**：图查询 ⊃ 拓扑，多出来的恰好是兄弟节点。
    """
    import asyncio

    from aiops_datasource_mcp_server.backends import cmdb

    topo = asyncio.run(cmdb.get_service_topology("order-service", 2))
    topo_ids = {f"app:{n['service']}" for n in topo["nodes"]}
    focus_ids = _ids(
        gq.query_graph(graph, node_id="app:order-service", hops=2, edge_types=["calls"])
    )

    assert topo_ids < focus_ids, "2 跳下图查询应严格包含拓扑结果"
    assert focus_ids - topo_ids == {"app:user-service"}, (
        "多出来的应当只有兄弟节点（gateway 的另一个下游）"
    )


def test_focus_unknown_node_id_rejected(graph) -> None:
    with pytest.raises(AppError, match="未知节点 id"):
        gq.query_graph(graph, node_id="app:no-such-thing")


# ======================================================================
# 其它
# ======================================================================
def test_all_attributes_reachable_including_ones_topology_omits(graph) -> None:
    """`_info()` 的六键投影不含 runtime / repo，图查询要把它们露出来。

    否则这两个字段就是**存了但没处读**的死数据。
    """
    r = gq.query_graph(graph, node_id="app:order-service", hops=0)
    node = r["nodes"][0]
    assert node["attributes"]["runtime"] == "k8s"
    assert node["refs"]["repo_ref"] == "codebase:aiops-test-order-service"


def test_degree_is_computed(graph) -> None:
    r = gq.query_graph(graph, node_types=["app"])
    by_id = {n["id"]: n for n in r["nodes"]}
    # order-service: 上游 gateway(1) + 下游 5 + portfolio_link(1) = 7
    assert by_id["app:order-service"]["degree"] == 7
    # warranty-service: 上游 order(1) + portfolio_link(1) = 2
    assert by_id["app:warranty-service"]["degree"] == 2


def test_include_facets_can_be_disabled(graph) -> None:
    r = gq.query_graph(graph, include_facets=False)
    assert "facets" not in r
    assert "unavailable_dimensions" not in r
