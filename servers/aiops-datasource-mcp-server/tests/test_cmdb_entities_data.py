"""黄金迁移测试：实体文件必须与迁移前的 ``cmdb.py`` 字面量**逐字段一致**。

**为什么需要它**：``test_cmdb_backend.py::test_catalog_is_rich_enough`` 只抽查
``owner != "unknown"`` / ``tier ∈ {edge,core,support}``——60 个属性字符串里打错一个能
溜过去。本文件把整份历史数据冻成期望值，迁移是否无损由它说了算。

期望值是从迁移前的 ``backends/cmdb.py`` 的 ``_SERVICES`` / ``_DEPENDS_ON`` 逐字抄下来的，
**不要为了迁就实现而修改它**——改了它就等于放弃了这个测试的全部价值。
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from aiops_datasource_mcp_server.backends import entity_graph as eg

# ----------------------------------------------------------------------
# 迁移前的历史字面量（冻结）
# ----------------------------------------------------------------------
EXPECTED_SERVICES: dict[str, dict] = {
    "gateway-service": {
        "namespace": "order", "owner": "网关团队", "tier": "edge",
        "tech": "Spring Cloud Gateway", "runtime": "k8s", "criticality": "high",
        "repo": "aiops-test-gateway-service",
    },
    "order-service": {
        "namespace": "order", "owner": "交易履约", "tier": "core",
        "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "critical",
        "repo": "aiops-test-order-service",
    },
    "warranty-service": {
        "namespace": "order", "owner": "售后保障", "tier": "core",
        "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "high",
        "repo": "aiops-test-warranty-service",
    },
    "payment-service": {
        "namespace": "payment", "owner": "支付团队", "tier": "core",
        "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "critical",
        "repo": "payment-service",
    },
    "inventory-service": {
        "namespace": "inventory", "owner": "库存团队", "tier": "core",
        "tech": "Go", "runtime": "k8s", "criticality": "high",
        "repo": "inventory-service",
    },
    "pricing-service": {
        "namespace": "order", "owner": "定价团队", "tier": "core",
        "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "medium",
        "repo": "pricing-service",
    },
    "logistics-service": {
        "namespace": "logistics", "owner": "履约调度", "tier": "support",
        "tech": "Go", "runtime": "k8s", "criticality": "medium",
        "repo": "logistics-service",
    },
    "notification-service": {
        "namespace": "common", "owner": "平台基础", "tier": "support",
        "tech": "Python / FastAPI", "runtime": "k8s", "criticality": "low",
        "repo": "notification-service",
    },
    "user-service": {
        "namespace": "account", "owner": "用户中心", "tier": "core",
        "tech": "Java / Spring Boot", "runtime": "k8s", "criticality": "high",
        "repo": "user-service",
    },
    "audit-service": {
        "namespace": "common", "owner": "安全合规", "tier": "support",
        "tech": "Go", "runtime": "k8s", "criticality": "low",
        "repo": "audit-service",
    },
}

EXPECTED_DEPENDS_ON: dict[str, list[tuple[str, str]]] = {
    "gateway-service": [("order-service", "http"), ("user-service", "http")],
    "order-service": [
        ("warranty-service", "rpc"),
        ("payment-service", "rpc"),
        ("inventory-service", "rpc"),
        ("pricing-service", "rpc"),
        ("notification-service", "mq"),
    ],
    "payment-service": [("audit-service", "mq"), ("notification-service", "mq")],
    "inventory-service": [("logistics-service", "http"), ("pricing-service", "rpc")],
    "logistics-service": [("notification-service", "mq")],
    "user-service": [("audit-service", "mq")],
    "warranty-service": [],
    "pricing-service": [],
    "notification-service": [],
    "audit-service": [],
}


# ----------------------------------------------------------------------
# 迁移无损
# ----------------------------------------------------------------------
def test_services_match_legacy_literal(clear_settings_cache) -> None:
    """10 个服务的全部 7 个字段（6 属性 + repo）逐字一致。"""
    graph = eg.get_graph()
    assert set(graph.services) == set(EXPECTED_SERVICES)
    for name, expected in EXPECTED_SERVICES.items():
        assert graph.services[name] == expected, f"{name} 与历史字面量不一致"


def test_depends_on_matches_legacy_literal(clear_settings_cache) -> None:
    """13 条依赖边逐条一致，且 app 索引齐全（含无出边的服务）。"""
    graph = eg.get_graph()
    assert set(graph.depends_on) == set(EXPECTED_DEPENDS_ON)
    total = 0
    for caller, expected in EXPECTED_DEPENDS_ON.items():
        assert graph.depends_on[caller] == expected, f"{caller} 的出边与历史字面量不一致"
        total += len(expected)
    assert total == 13, f"依赖边总数应为 13，实际 {total}"


def test_repo_by_app_matches_legacy_literal(clear_settings_cache) -> None:
    graph = eg.get_graph()
    assert graph.repo_by_app == {n: v["repo"] for n, v in EXPECTED_SERVICES.items()}


# ----------------------------------------------------------------------
# 文件本身
# ----------------------------------------------------------------------
def test_default_file_resolves_independent_of_cwd(clear_settings_cache) -> None:
    """默认文件走**包内路径**，与 cwd 无关。

    这条守着打包：若 ``data/*.json`` 没进 wheel，或路径被写成 cwd 相对，
    换个工作目录跑就会 fail-closed——而那种失败在 CI 里才会暴露。
    """
    resolved = Path(eg.resolve_cmdb_path())
    assert resolved.is_absolute(), resolved
    assert resolved.is_file(), resolved

    packaged = resources.files("aiops_datasource_mcp_server").joinpath(
        "data", "cmdb-entities.json"
    )
    assert resolved == Path(str(packaged))


def test_all_twelve_node_types_present(clear_settings_cache) -> None:
    """12 个节点类型键恒存在；未录入的是显式 []，不是缺键。"""
    raw = json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))
    declared = {t["key"] for t in raw["ontology"]["node_types"]}
    assert len(declared) == 12
    assert set(raw["nodes"]) == declared
    assert raw["nodes"]["journey"] == []
    assert raw["nodes"]["incident"] == []
    assert raw["nodes"]["change"] == []


def test_static_tags_and_derived_metrics_are_disjoint(clear_settings_cache) -> None:
    """静态标签与派生指标互斥——派生指标不允许出现在任何节点的 tags 里。"""
    graph = eg.get_graph()
    assert graph.key_attribute_keys & graph.derived_metric_keys == set()
    assert len(graph.key_attribute_keys) == 4
    assert len(graph.derived_metric_keys) == 3
    for node in graph.nodes.values():
        assert set(node["tags"]) <= graph.key_attribute_keys, node["id"]
