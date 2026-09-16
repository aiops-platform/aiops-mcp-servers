"""CMDB 后端：服务目录 + N 跳拓扑 + 仓库定位。

拓扑方向语义是本模块最容易写错的地方——按"遍历方向"标会得到错误的上下游
（从上游节点再往下走会到**兄弟节点**），必须相对**起点**定义。
"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.backends import cmdb
from aiops_datasource_mcp_server.backends import entity_graph as eg


def _by_name(result: dict) -> dict[str, dict]:
    return {n["service"]: n for n in result["nodes"]}


# ----------------------------------------------------------------------
# 拓扑：方向与跳数
# ----------------------------------------------------------------------
async def test_topology_direction_is_relative_to_origin(env) -> None:
    """order-service 视角：gateway 是上游（调它），其余是下游（它调）。"""
    r = await cmdb.get_service_topology("order-service", 1)
    nodes = _by_name(r)
    assert nodes["gateway-service"]["direction"] == "upstream"
    assert nodes["order-service"]["direction"] == "self"
    for svc in ("warranty-service", "payment-service", "inventory-service",
                "pricing-service", "notification-service"):
        assert nodes[svc]["direction"] == "downstream", svc
    assert r["upstream"] == ["gateway-service"]
    assert "warranty-service" in r["downstream"]


async def test_topology_does_not_mislabel_siblings_as_downstream(env) -> None:
    """反例回归：从 warranty 出发，2 跳应沿**调用链回到上游**（order → gateway），
    而不是把 order 的其它下游（payment 等兄弟节点）错标成 warranty 的下游。
    """
    r = await cmdb.get_service_topology("warranty-service", 2)
    nodes = _by_name(r)
    assert set(nodes) == {"warranty-service", "order-service", "gateway-service"}
    assert nodes["order-service"]["direction"] == "upstream"
    assert nodes["gateway-service"]["direction"] == "upstream"
    assert nodes["gateway-service"]["distance"] == 2
    assert r["downstream"] == []          # warranty 不调任何人
    assert set(r["upstream"]) == {"order-service", "gateway-service"}


async def test_topology_respects_hops_limit(env) -> None:
    one = await cmdb.get_service_topology("order-service", 1)
    two = await cmdb.get_service_topology("order-service", 2)
    assert max(n["distance"] for n in one["nodes"]) == 1
    assert max(n["distance"] for n in two["nodes"]) == 2
    # audit/logistics 只在 2 跳内可达（经 payment/inventory）
    assert "audit-service" not in _by_name(one)
    assert "audit-service" in _by_name(two)


async def test_topology_hops_zero_returns_only_origin(env) -> None:
    r = await cmdb.get_service_topology("order-service", 0)
    assert [n["service"] for n in r["nodes"]] == ["order-service"]
    assert r["edges"] == []
    assert "没有关联服务" in r["summary"]


async def test_topology_unknown_service_is_not_an_error(env) -> None:
    """未知服务不抛错——拓扑查询可能是探索性的，返回 known=False 让调用方判断。"""
    r = await cmdb.get_service_topology("no-such-service", 2)
    assert r["known"] is False
    assert r["node_count"] == 1
    assert "不在 CMDB 目录中" in r["summary"]


async def test_topology_negative_hops_rejected(env) -> None:
    from aiops_datasource_mcp_server.errors import AppError

    with pytest.raises(AppError):
        await cmdb.get_service_topology("order-service", -1)


async def test_topology_edges_are_directed(env) -> None:
    """边按调用方向记录，且只含可达节点之间的边。"""
    r = await cmdb.get_service_topology("order-service", 1)
    edges = {(e["from"], e["to"]) for e in r["edges"]}
    assert ("gateway-service", "order-service") in edges   # gateway 调 order
    assert ("order-service", "warranty-service") in edges
    assert ("order-service", "gateway-service") not in edges  # 方向不能反
    # payment→audit 的边不在 1 跳结果里（audit 未达）
    assert not any(e["to"] == "audit-service" for e in r["edges"])


async def test_topology_nodes_carry_catalog_info(env) -> None:
    r = await cmdb.get_service_topology("order-service", 1)
    node = _by_name(r)["payment-service"]
    assert node["owner"] == "支付团队"
    assert node["tier"] == "core"
    assert node["namespace"] == "payment"
    assert node["criticality"] == "critical"


# ----------------------------------------------------------------------
# 仓库定位
# ----------------------------------------------------------------------
async def test_locate_repo_remote_url_by_default(env) -> None:
    """未配本地 root → 返回远端 URL（**不得**出现个人绝对路径）。"""
    r = await cmdb.locate_repo("order-service")
    assert r["found"] is True
    assert r["repo_url"] == "https://github.com/acme-aiops/aiops-test-order-service"
    assert "/Users/" not in r["repo_url"]


async def test_locate_repo_local_root_override(monkeypatch, clear_settings_cache) -> None:
    """配了 DATASOURCE_REPO_ROOT → 返回 file:// 本地路径（testbed 联调）。"""
    monkeypatch.setenv("DATASOURCE_REPO_ROOT", "/tmp/testbed/services")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    r = await cmdb.locate_repo("warranty-service")
    assert r["repo_url"] == "file:///tmp/testbed/services/aiops-test-warranty-service"


async def test_locate_repo_unknown_service(env) -> None:
    """未收录 → found=false 并给出可用清单（防止调用方编造仓库）。"""
    r = await cmdb.locate_repo("no-such-service")
    assert r["found"] is False
    assert r["repo_url"] == ""
    assert "order-service" in r["known_services"]


def test_catalog_is_rich_enough(env) -> None:
    """运行时目录的规模与字段完整度。

    ⚠️ **只对 ``source=kubernetes`` 的 app 断言**：目录自 OTR 整合后含 60 个应用，
    其中 50 个是业务盘点来的（``source=otr-inventory``），那份数据**根本没有**
    owner / namespace / 拓扑层——它们的这些字段就该是 ``unknown``。
    对它们断言"字段齐全"等于逼人编造。
    """
    graph = eg.get_graph()
    k8s = [
        name for name, meta in graph.services.items()
        if meta.get("source", "kubernetes") == "kubernetes"
    ]
    assert len(k8s) >= 10
    for svc in k8s:
        info = cmdb._info(svc)
        assert info["owner"] != "unknown", svc
        assert info["namespace"] != "unknown", svc
        assert info["tier"] in {"edge", "core", "support"}, svc


def test_otr_inventory_apps_report_unknown_not_fabricated(env) -> None:
    """OTR 业务应用的运行时字段是 ``unknown``（未知且不编造），而**不是**编出来的值。

    这条钉住的是"未知不编造"这个不变量本身。它很容易被后人"修好"——看到一堆
    ``owner: unknown`` 就顺手填个默认值，于是 ``get_service_topology`` 开始自信地
    返回假归属。
    """
    graph = eg.get_graph()
    otr = [
        name for name, meta in graph.services.items()
        if meta.get("source") == "otr-inventory"
    ]
    assert len(otr) == 50
    for svc in otr[:5]:
        info = cmdb._info(svc)
        assert info["owner"] == "unknown", svc
        assert info["tier"] == "unknown", svc
        # 业务分级是有的——它是 OTR 真正的数据，落在另一个字段上
        assert graph.services[svc]["business_tier"] in (1, 2, 3), svc
