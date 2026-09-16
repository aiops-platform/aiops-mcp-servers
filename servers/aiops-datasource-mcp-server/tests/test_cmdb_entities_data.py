"""黄金迁移测试：**迁移基线**必须与迁移前的 ``cmdb.py`` 字面量**逐字段一致**。

**为什么需要它**：``test_cmdb_backend.py::test_catalog_is_rich_enough`` 只抽查
``owner != "unknown"`` / ``tier ∈ {edge,core,support}``——60 个属性字符串里打错一个能
溜过去。本文件把整份历史数据冻成期望值，迁移是否无损由它说了算。

期望值是从迁移前的 ``backends/cmdb.py`` 的 ``_SERVICES`` / ``_DEPENDS_ON`` 逐字抄下来的，
**不要为了迁就实现而修改它**——改了它就等于放弃了这个测试的全部价值。

## ⚠️ 断言的对象是**冻结基线**，不是包内那份活文件

CMDB 自 2026-09-16 起是**可编辑**的（编辑页 + ``/admin/cmdb/**``），包内那份活文件
会随人工编辑不断变化。若把"迁移无损"这条断言指向活文件，它就会在**每次有人改数据时**
变红——而改数据恰恰是这个功能的目的。那样的测试最后只会被人删掉。

所以这里分两类，各自的职责写在各自的 docstring 里：

| | 看哪份数据 | 断言什么 |
|---|---|---|
| 本节「迁移无损」 | ``tests/fixtures/cmdb-migration-baseline.json``（冻结） | 逐字段、逐条边 |
| 本节「文件本身」 | 包内活文件 | **只断言结构与不变式**，不钉数量 |

基线是**导入器的产出快照**（``scripts/import_otr_tenant.py`` 跑完那一刻），
不包含其后的人工编辑。要更新它，是明确决定"把当前状态认定为新基线"，
而不是为了让测试变绿。
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
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
def test_services_match_legacy_literal(baseline_cmdb) -> None:
    """10 个服务的**历史** 7 个字段（6 属性 + repo）逐字一致。

    v5.7 给 app 加了 `kind`（与可选的 `business_role`），所以用**子集比对**而不是全等：
    迁移的回归价值在于"历史字段一个都没变、一个都没少"，而不在于"没有新增字段"。
    用全等会让每次合理的 schema 演进都触发假失败，最终逼人把这条测试删掉。

    同理，OTR 整合后目录从 10 个应用涨到 60 个（50 个 `source=otr-inventory` 的业务
    应用），这里也用超集而不是相等——**断言仍然是"历史 10 个一个不少、逐字段未变"，
    价值没有被稀释**，只是不再额外要求"目录里没有别的东西"。
    """
    graph = eg.get_graph()
    assert set(EXPECTED_SERVICES) <= set(graph.services)
    for name, expected in EXPECTED_SERVICES.items():
        for key, val in expected.items():
            assert graph.services[name][key] == val, f"{name}.{key} 与历史字面量不一致"


def test_every_app_declares_kind(clear_settings_cache) -> None:
    """v5.7 新增：每个 app 必须声明 kind，用于区分「应用」与「云环境」。"""
    graph = eg.get_graph()
    for name, entry in graph.services.items():
        assert entry["kind"] in {"application", "environment"}, name


def test_depends_on_matches_legacy_literal(baseline_cmdb) -> None:
    """13 条依赖边逐条一致，且 app 索引齐全（含无出边的服务）。

    同 ``test_services_match_legacy_literal``：OTR 的 50 个业务应用也进 ``depends_on``
    索引（它们只是没有出边），故用超集。**"这 10 个调用方的出边逐条未变"仍然被钉死。**
    """
    graph = eg.get_graph()
    assert set(EXPECTED_DEPENDS_ON) <= set(graph.depends_on)
    total = 0
    for caller, expected in EXPECTED_DEPENDS_ON.items():
        assert graph.depends_on[caller] == expected, f"{caller} 的出边与历史字面量不一致"
        total += len(expected)
    assert total == 13, f"依赖边总数应为 13，实际 {total}"


def test_repo_by_app_matches_legacy_literal(baseline_cmdb) -> None:
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
    """**活文件**：12 个节点类型键恒存在；未录入的是显式 []，不是缺键。

    业务层自 OTR 整合起**不再空置**。但 ``incident`` / ``change`` **仍然必须为空**
    ——它们属于事件覆盖层（``DATASOURCE_INCIDENTS_PATH``）。放进主文件会有一个静默
    副作用：``graph_query`` 用"事件桶是否非空"判断 ``have_incident_data``，非空就
    启用事件匹配并把 ``degraded`` 翻成 False，等于拿一次风暴冒充整份事件语料。
    见 docs/cmdb-entities.md §6。

    ⚠️ 其余类型**只断言非空、不断言数量**——活文件可编辑，"业务层有几个 portfolio"
    是用户可以随时改的，不是不变量。数量由冻结基线那边负责。
    """
    raw = json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))
    declared = {t["key"] for t in raw["ontology"]["node_types"]}
    assert len(declared) == 12
    assert set(raw["nodes"]) == declared

    # 业务层已录入（非空）——这三条是不变量：业务层的存在是 OTR 整合的目的
    assert raw["nodes"]["enterprise"], "企业层不应为空"
    assert raw["nodes"]["journey"], "旅程层不应为空"
    assert raw["nodes"]["portfolio"], "业务领域层不应为空"

    # 事件桶必须为空（见 docstring）
    assert raw["nodes"]["incident"] == []
    assert raw["nodes"]["change"] == []


def test_otr_overlay_carries_the_event_data(monkeypatch, clear_settings_cache) -> None:
    """OTR 的事件数据在**覆盖层文件**里，且默认不生效、指过去才生效。

    这条守着"事件数据必须走覆盖层"这个分层。上面那条只断言主文件的事件桶是空的——
    若把覆盖层也一起删掉，那条就退化成永远为真的空断言。这里走**真实的**加载路径
    （``DATASOURCE_INCIDENTS_PATH`` → ``get_effective_graph``），把整条链路钉死。
    """
    overlay = Path(eg.resolve_cmdb_path()).with_name("cmdb-incidents-otr.json")
    assert overlay.is_file(), f"缺少事件覆盖层：{overlay}"
    doc = json.loads(overlay.read_text(encoding="utf-8"))
    assert len(doc["nodes"]["incident"]) == 1
    assert doc["edges"][0]["type"] == "incident_cluster_app"
    assert doc["edges"][0]["to"] == "app:xentry-workshop"
    # ontology 由主图提供——覆盖层重复声明就会漂移（docs §6）
    assert "ontology" not in doc

    from aiops_datasource_mcp_server.config import get_settings

    base = eg.get_graph()
    # 默认（未配置路径）：没有事件数据 = 合法状态，不是配置错误
    assert eg.get_effective_graph().node_count == base.node_count

    # 指过去：合并进来，且节点数 +1
    monkeypatch.setenv("DATASOURCE_INCIDENTS_PATH", str(overlay))
    get_settings.cache_clear()
    eg.clear_overlay_cache()
    merged = eg.get_effective_graph()
    assert merged.node_count == base.node_count + 1
    assert merged.nodes["incident:inc-mbr-77104"]["type"] == "incident"


def test_static_tags_and_derived_metrics_are_disjoint(clear_settings_cache) -> None:
    """静态标签与派生指标互斥——派生指标不允许出现在任何节点的 tags 里。"""
    graph = eg.get_graph()
    assert graph.key_attribute_keys & graph.derived_metric_keys == set()
    assert len(graph.key_attribute_keys) == 4
    assert len(graph.derived_metric_keys) == 3
    for node in graph.nodes.values():
        assert set(node["tags"]) <= graph.key_attribute_keys, node["id"]


# ----------------------------------------------------------------------
# 不变量：一个 app 最多一个 codebase
# ----------------------------------------------------------------------
def test_current_data_honours_one_repo_per_app(clear_settings_cache) -> None:
    """现有数据满足「一个 app 一个仓库」——不变量不是只写给测试的反例用的。"""
    graph = eg.get_graph()
    seen: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge["type"] != "app_codebase":
            continue
        app = edge["from"] if edge["from"].startswith("app:") else edge["to"]
        cb = edge["to"] if app == edge["from"] else edge["from"]
        seen.setdefault(app, []).append(cb)
    assert all(len(v) == 1 for v in seen.values()), seen
    assert len(graph.repo_by_app) == len(seen)


def test_second_codebase_on_same_app_is_rejected(tmp_path, clear_settings_cache) -> None:
    """同一 app 挂第二个 codebase → 拒绝加载。

    这是刻意的 fail-closed：``repo_by_app`` 是 1:1 的 dict（``repo_by_app[app] = cb``），
    两条边会让**后出现的那条静默胜出**——不报错、只取决于边在文件里的顺序。
    ``locate_repo`` 是"照着结果去翻代码"的工具，指向错仓库比找不到仓库危害大。
    """
    doc = json.loads(Path(eg.resolve_cmdb_path()).read_text(encoding="utf-8"))
    doc["nodes"]["codebase"].append(
        {
            "id": "codebase:order-service-alt", "type": "codebase",
            "name": "order-service-alt", "display_name": None, "description": None,
            "attributes": {}, "tags": [], "keywords": [], "refs": {}, "notes": None,
        }
    )
    doc["edges"].append(
        {
            "id": "e:app_codebase:dup", "type": "app_codebase",
            "from": "app:order-service", "to": "codebase:order-service-alt", "attributes": {},
        }
    )
    p = tmp_path / "dup-repo.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(eg.GraphLoadError, match="只能有一个仓库"):
        eg.load_entity_graph(str(p))
