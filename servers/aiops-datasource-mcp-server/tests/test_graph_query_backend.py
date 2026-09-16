"""图查询：四个维度的筛选、facets 现算、未知值 fail-closed、聚焦展开。

⚠️ **业务层在种子文件里是完全空置的**（v5.7 删掉了 6 个由 `namespace` 派生的假 Portfolio
——namespace 是 k8s 部署分组，不是业务领域）。所以业务维度的测试用 `biz_graph`
夹具自建数据；这也顺带演示了业务层节点该长什么样（`terms` 是**问题视角**的词）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.backends import graph_query as gq
from aiops_datasource_mcp_server.errors import AppError, ErrorCode


@pytest.fixture
def graph(baseline_cmdb) -> eg.EntityGraph:
    """冻结基线上的图。

    ⚠️ 不能读包内那份活文件：CMDB 现在是**可编辑**的，活文件会随人工编辑变化，
    数量类断言会在每次有人改数据时变红。见 conftest 的 BASELINE_CMDB。
    """
    return eg.get_graph()


def _strip_business(doc: dict) -> dict:
    """清掉种子文件里的业务层节点**与它们的边**，便于测试自建受控的业务层。

    ⚠️ 只清节点不清边会留下悬空端点——加载器会（正确地）报错。
    """
    biz = ("enterprise", "journey", "portfolio", "domain")
    biz_ids = {n["id"] for t in biz for n in doc["nodes"].get(t, [])}
    for t in biz:
        doc["nodes"][t] = []
    doc["edges"] = [
        e for e in doc["edges"] if e["from"] not in biz_ids and e["to"] not in biz_ids
    ]
    return doc


@pytest.fixture
def biz_graph(tmp_path, baseline_cmdb) -> eg.EntityGraph:
    """种子文件（业务层已清空）+ 一小组自建业务层节点与它们的归属边。"""
    doc = _strip_business(
        json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))
    )
    doc["nodes"]["portfolio"] = [
        {
            "id": "portfolio:order-transaction", "type": "portfolio",
            "name": "order-transaction", "display_name": "订单交易",
            "description": "接收下单请求、维护订单状态、计算价格",
            "keywords": ["订单", "下单", "下不了单", "价格", "算价"],
            "attributes": {}, "tags": [], "refs": {},
        }
    ]
    doc["nodes"]["domain"] = [
        {
            "id": "domain:ticket-print", "type": "domain", "name": "ticket-print",
            "display_name": "工单打印",
            "description": "打印工单并跟踪打印结果",
            "keywords": ["打印工单", "打印没反应", "打不出单"],
            "attributes": {}, "tags": [], "refs": {},
        }
    ]
    doc["nodes"]["journey"] = [
        {
            "id": "journey:after-sales", "type": "journey", "name": "after-sales",
            "display_name": "售后服务",
            "description": "售后受理与跟踪",
            "keywords": ["售后", "退货", "换货"],
            "attributes": {"capability": "After-sales Service"},
            "tags": [], "refs": {},
        }
    ]
    doc["edges"] += [
        {"id": "e:pl-order", "type": "portfolio_link",
         "from": "portfolio:order-transaction", "to": "app:order-service", "attributes": {}},
        {"id": "e:pl-pricing", "type": "portfolio_link",
         "from": "portfolio:order-transaction", "to": "app:pricing-service", "attributes": {}},
        {"id": "e:dl-print", "type": "domain_link",
         "from": "domain:ticket-print", "to": "app:order-service", "attributes": {}},
        {"id": "e:jl-1", "type": "journey_link",
         "from": "journey:after-sales", "to": "portfolio:order-transaction", "attributes": {}},
    ]
    p = tmp_path / "biz.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return eg.load_entity_graph(str(p))


def _ids(result: dict) -> set[str]:
    return {n["id"] for n in result["nodes"]}


# ======================================================================
# 基线
# ======================================================================
def test_no_filter_returns_whole_graph(graph) -> None:
    r = gq.query_graph(graph)
    # 业务层 15（1 enterprise + 2 journey + 11 portfolio + 1 domain）
    #   + app 60 + codebase 10 + agent 17
    assert r["matched"]["nodes"] == 102
    # 2 enterprise_journey + 11 journey_link + 60 portfolio_link
    #   + 13 calls + 10 app_codebase + 1 domain_link
    assert r["matched"]["edges"] == 97
    assert r["counts"]["node_types"]["app"] == 60
    assert r["counts"]["node_types"]["journey"] == 2     # OTR 整合后不再为 0
    assert r["counts"]["edge_types"]["calls"] == 13
    assert r["counts"]["edge_types"]["support"] == 0


def test_facets_cover_all_four_dimensions(graph) -> None:
    r = gq.query_graph(graph)
    f = r["facets"]
    assert len(f["node_types"]) == 12           # 含 v5.7 新增的 domain
    assert len(f["edge_types"]) == 13
    assert len(f["key_attributes"]) == 4
    assert {t["key"] for t in f["node_types"]} == {
        "enterprise", "journey", "portfolio", "domain", "app",
        "team", "agent", "tool", "codebase", "wiki", "incident", "change",
    }
    # 业务层来自 OTR 租户，11 个业务领域，不再整维为空
    assert {p["key"] for p in f["portfolios"]} == {
        "campaign", "lead", "consulation", "offer-order", "handover", "used-car",
        "work-order", "workshop", "warranty", "parts", "accounting",
    }
    # work-order 下 5 个 OTR 业务应用 + 桥接过来的 order-service
    assert {p["key"]: p["count"] for p in f["portfolios"]}["work-order"] == 6
    # 计数是现算的，不是存储的
    assert {a["key"]: a["count"] for a in f["key_attributes"]}["tier1"] == 2
    # holiday_critical 原本为 0（种子数据无人用它）；OTR 的 apps 带来了真实取值
    assert {a["key"]: a["count"] for a in f["key_attributes"]}["holiday_critical"] == 21


def test_derived_metrics_are_declared_but_unavailable(graph) -> None:
    """派生指标只声明、不可用——它们带时间窗口，不进静态文件。"""
    r = gq.query_graph(graph)
    assert len(r["derived_metrics_declared"]) == 3
    assert all(d["available"] is False for d in r["derived_metrics_declared"])
    assert {d["key"] for d in r["derived_metrics_declared"]} == {
        "top10_incidents_18m", "top10_changes_13m", "emergency_changes"
    }


def test_every_edge_type_declares_a_layer(graph) -> None:
    """分层是强制的（business 层禁 app—app 的落点），不是可选注解。"""
    for spec in graph.ontology.edge_types:
        assert spec.layer in {"business", "runtime", "support", "event"}, spec.key
    assert next(e for e in graph.ontology.edge_types if e.key == "calls").layer == "runtime"


# ======================================================================
# 四个维度
# ======================================================================
def test_node_type_dimension(graph) -> None:
    r = gq.query_graph(graph, node_types=["app"])
    assert r["matched"]["nodes"] == 60   # 50 个 OTR 业务应用 + 10 个 k8s 服务
    assert all(n["type"] == "app" for n in r["nodes"])
    assert r["matched"]["edges"] == 13   # 只剩 calls（app—codebase 的一端是 codebase）


def test_portfolio_dimension(biz_graph) -> None:
    """选一个业务领域 → 该域下的应用 + 域节点本身。"""
    r = gq.query_graph(biz_graph, portfolios=["order-transaction"])
    assert _ids(r) == {
        "portfolio:order-transaction", "app:order-service", "app:pricing-service",
    }
    assert "app:payment-service" not in _ids(r)


def test_domain_dimension(biz_graph) -> None:
    """domain 与 portfolio **平行**直连 app——它是独立的第二条召回路径。"""
    r = gq.query_graph(biz_graph, node_types=["domain"])
    assert _ids(r) == {"domain:ticket-print"}


def test_portfolio_and_domain_are_independent_paths(biz_graph) -> None:
    """同一 app 可被 portfolio 与 domain **分别**命中——这是交叉验证的素材。

    注意 `domain:ticket-print` 与 `portfolio:order-transaction` 之间**没有边**：
    它们各自直连 app，互不隶属。
    """
    via_portfolio = _ids(gq.query_graph(biz_graph, portfolios=["order-transaction"]))
    via_domain = {
        n["id"] for n in gq.query_graph(biz_graph, node_types=["domain"])["nodes"]
    }
    # 从 domain 下钻一跳应落到 app:order-service，与 portfolio 路径重合
    drilled = gq.query_graph(biz_graph, node_id="domain:ticket-print", hops=1)
    assert "app:order-service" in _ids(drilled)
    assert "domain:ticket-print" in via_domain
    assert "app:order-service" in via_portfolio


def test_key_attribute_dimension(graph) -> None:
    r = gq.query_graph(graph, key_attributes=["tier1"])
    assert _ids(r) == {"app:order-service", "app:payment-service"}


def test_declared_but_unused_key_attribute_returns_empty_not_error(graph) -> None:
    """`manhattan_wms_spotlight` 是**声明过的**合法取值——过滤它返回空是正常语义，不报错。

    这与"传了不存在的取值"必须区分开：前者是数据尚未录入，后者是拼写错误。

    ⚠️ 这条测试**必须挑一个当前无人使用的标签**。原先挑的是 `holiday_critical`，
    OTR 整合后它有 21 个节点在用，前提就不成立了（测试会红，但红得毫无意义）。
    换标签时请先确认它确实为空——否则这条测试会退化成在验证"有数据时返回有数据"。
    四个静态标签里现在只有这一个仍为空。
    """
    graph_ = eg.get_graph()
    used = {
        tag for node in graph_.nodes.values() for tag in node["tags"]
    }
    assert "manhattan_wms_spotlight" not in used, (
        "本测试依赖 manhattan_wms_spotlight 无人使用；它一旦被用上，请换一个仍为空的标签"
    )

    r = gq.query_graph(graph, key_attributes=["manhattan_wms_spotlight"])
    assert r["matched"]["nodes"] == 0
    assert r["summary"].startswith("命中 0 个节点")


def test_edge_type_dimension(graph) -> None:
    r = gq.query_graph(graph, edge_types=["app_codebase"])
    assert r["matched"]["edges"] == 10
    assert all(e["type"] == "app_codebase" for e in r["edges"])


def test_dimensions_compose(biz_graph) -> None:
    """维度可叠加：订单交易业务域 ∩ 带 tier1 标签。"""
    r = gq.query_graph(biz_graph, portfolios=["order-transaction"], key_attributes=["tier1"])
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
    """`cross_journey_link` 是 v5.7 删掉的边类型——现在应被拒并列合法值。"""
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, edge_types=["cross_journey_link"])
    assert "calls" in ei.value.details["valid"]


def test_empty_portfolio_vocabulary_says_not_a_typo(tmp_path, clear_settings_cache) -> None:
    """业务层空置时传 portfolio → 必须说「不是拼写问题，是数据未录入」。

    否则 agent 会换个拼写重试一万次——而问题在于数据还没录，不在写法。

    种子文件现在有 2 个 portfolio，所以这个分支要**自建一个业务层为空的图**才走得到。
    """
    doc = _strip_business(
        json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))
    )
    p = tmp_path / "no_biz.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    empty = eg.load_entity_graph(str(p))
    with pytest.raises(AppError, match="不是拼写问题"):
        gq.query_graph(empty, portfolios=["Retail"])


def test_unknown_portfolio_lists_valid_values(biz_graph) -> None:
    """有数据时走的是「列合法值」分支——两条分支必须能区分开。"""
    with pytest.raises(AppError) as ei:
        gq.query_graph(biz_graph, portfolios=["Retail"])
    assert "order-transaction" in ei.value.details["valid"]


def test_unknown_key_attribute_rejected(graph) -> None:
    with pytest.raises(AppError) as ei:
        gq.query_graph(graph, key_attributes=["top10_incidents_18m"])
    assert "tier1" in ei.value.details["valid"]


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
    - 图查询按**无向邻域**展开。它要跨 13 类边，其中 ``portfolio_link`` / ``app_codebase``
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
    """`_info()` 的六键投影不含 runtime / kind，图查询要把它们露出来。

    否则这两个字段就是**存了但没处读**的死数据。
    """
    r = gq.query_graph(graph, node_id="app:order-service", hops=0)
    node = r["nodes"][0]
    assert node["attributes"]["runtime"] == "k8s"
    assert node["attributes"]["kind"] == "application"


def test_repo_is_an_edge_not_a_ref(graph) -> None:
    """v5.7：仓库归属由 ``refs.repo_ref`` 提升为 ``app_codebase`` 边。

    但 `repo_by_app` 派生索引保持同形，所以 `locate_repo` 的对外契约不变。
    """
    r = gq.query_graph(graph, node_id="app:order-service", hops=1)
    assert "refs" in r["nodes"][0] and r["nodes"][0]["refs"] == {}
    edge = next(
        e for e in r["edges"]
        if e["type"] == "app_codebase" and e["from"] == "app:order-service"
    )
    assert edge["to"] == "codebase:aiops-test-order-service"
    assert graph.repo_by_app["order-service"] == "aiops-test-order-service"


def test_degree_is_computed(graph) -> None:
    r = gq.query_graph(graph, node_types=["app"])
    by_id = {n["id"]: n for n in r["nodes"]}
    # order-service: calls 6（上游 gateway 1 + 下游 5）+ app_codebase 1
    #                + portfolio_link 1（work-order）+ domain_link 1（work-order-ops）= 9
    assert by_id["app:order-service"]["degree"] == 9
    # warranty-service: calls 1（上游 order）+ app_codebase 1 + portfolio_link 1（warranty）= 3
    assert by_id["app:warranty-service"]["degree"] == 3


def test_include_facets_can_be_disabled(graph) -> None:
    r = gq.query_graph(graph, include_facets=False)
    assert "facets" not in r
    assert "unavailable_dimensions" not in r
