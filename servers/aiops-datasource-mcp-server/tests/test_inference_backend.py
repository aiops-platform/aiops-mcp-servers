"""候选服务推断：三档证据、置信/影响两轴、诚实空结果。

这个工具最容易出的错是**编造服务名**。下面每条测试都在守这条线：候选必须能追溯到
具体事实（reasons 非空），找不到就说找不到。
"""
from __future__ import annotations

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.backends import graph_query as gq
from aiops_datasource_mcp_server.errors import AppError


@pytest.fixture
def graph(clear_settings_cache) -> eg.EntityGraph:
    return eg.get_graph()


def _by_service(result: dict) -> dict[str, dict]:
    return {c["service"]: c for c in result["candidate_apps"]}


# ======================================================================
# E1：症状服务 + 拓扑扩展
# ======================================================================
def test_symptom_service_is_high_confidence(graph) -> None:
    r = gq.infer_candidates(graph, problem="warranty 页面报错", services=["warranty-service"])
    cands = _by_service(r)
    assert cands["warranty-service"]["confidence"] == "high"
    assert cands["warranty-service"]["distance_from_symptom"] == 0
    assert "symptom_services" in r["evidence_used"]


def test_topology_neighbour_is_medium_confidence(graph) -> None:
    """warranty 只被 order 调用，1 跳邻居就是 order。"""
    r = gq.infer_candidates(graph, problem="x", services=["warranty-service"], max_hops=1)
    cands = _by_service(r)
    assert cands["order-service"]["confidence"] == "medium"
    assert cands["order-service"]["distance_from_symptom"] == 1
    assert any("warranty-service" in reason for reason in cands["order-service"]["reasons"])


def test_two_hops_reaches_gateway(graph) -> None:
    r = gq.infer_candidates(graph, problem="x", services=["warranty-service"], max_hops=2)
    assert "gateway-service" in _by_service(r)


def test_max_hops_zero_disables_expansion(graph) -> None:
    r = gq.infer_candidates(graph, problem="x", services=["warranty-service"], max_hops=0)
    assert set(_by_service(r)) == {"warranty-service"}


def test_unresolved_services_are_reported_not_dropped(graph) -> None:
    """未收录的服务名必须出现在 unresolved 里——静默丢弃会让调用方以为查过了。"""
    r = gq.infer_candidates(graph, problem="x", services=["no-such-svc", "order-service"])
    assert r["unresolved_services"] == ["no-such-svc"]
    assert "order-service" in _by_service(r)


# ======================================================================
# E2：文本匹配（弱兜底）与其局限
# ======================================================================
def test_text_match_on_service_name_is_medium_confidence(graph) -> None:
    """按服务名命中 → medium。

    ⚠️ 这里断言的是 ``name=order-service`` 这个**精确字符串**，不是早先那句
    ``any("name" in reason)``——后者是个假命题：reason 里的 ``namespace`` 恰好**包含子串**
    ``name``，所以它在"按服务名匹配其实从没工作过"的情况下也照样通过。
    """
    r = gq.infer_candidates(graph, problem="order-service 响应超时")
    cand = _by_service(r)["order-service"]
    assert cand["confidence"] == "medium"
    assert any("name=order-service" in reason for reason in cand["reasons"])


def test_chinese_two_char_keyword_matches(graph) -> None:
    """**回归测试**：中文双字关键词必须能命中。

    早先的 ``_MIN_TEXT_MATCH_LEN = 3`` 是为挡 ``tech="Go"`` 匹配 "going" 加的，
    却把 CMDB 里 33 个双字中文关键词（订单/支付/库存/价格…）**全部误杀**——
    中文是双字词密集的语言，3 字符门槛等于把中文匹配整个关掉。
    """
    r = gq.infer_candidates(graph, problem="支付超时")
    assert "payment-service" in {c["service"] for c in r["candidate_apps"]}


def test_business_layer_hit_drills_down_to_app(graph) -> None:
    """命中业务层节点 → 沿业务边**下钻**到 app。

    这是「问题域 → 方案域」的落点：问题描述常常根本不含服务名，
    「工单打印没反应」里一个服务名都没有。
    """
    r = gq.infer_candidates(graph, problem="工单打印没反应")
    cand = _by_service(r).get("order-service")
    assert cand is not None
    assert "domain" in cand["matched_layers"]
    assert "portfolio" in cand["matched_layers"]


def test_cross_validation_bumps_confidence(graph) -> None:
    """同一 app 被多条独立路径命中 → 升一档，并记进 reasons。

    「工单」同时命中 `portfolio:work-order` 与 `domain:work-order-ops`，两者都下钻到
    order-service——这是分层映射相对单层匹配最实在的增益。
    """
    r = gq.infer_candidates(graph, problem="工单打印没反应")
    cand = _by_service(r)["order-service"]
    assert cand["hit_paths"] >= 2
    assert any("交叉命中" in reason for reason in cand["reasons"])


def test_shared_attribute_hit_is_only_low_confidence(graph) -> None:
    """只命中共享属性（tech）→ low。

    6 个服务都跑 Java、3 个都在 order namespace——"命中"只说明它在这个集合里，
    不说明它与故障有关。这个区分防止"升级 Java"这类查询把 6 个服务都标成高置信。
    """
    r = gq.infer_candidates(graph, problem="升级 Java 版本")
    cands = _by_service(r)
    assert "order-service" in cands                       # Java 服务确实都在范围内
    assert cands["order-service"]["confidence"] == "low"  # 但证据是弱的
    assert cands["order-service"]["matched_layers"] == ["app"]


def test_text_match_on_namespace(graph) -> None:
    r = gq.infer_candidates(graph, problem="payment 相关告警")
    assert "payment-service" in _by_service(r)


def test_short_field_values_do_not_false_positive(graph) -> None:
    """`tech="Go"` 只有两个字符——不设下限的话，任何含 "go" 的英文都会误命中。"""
    r = gq.infer_candidates(graph, problem="logs are going crazy")
    assert r["candidate_apps"] == []


def test_unrelated_text_honestly_misses(graph) -> None:
    """**完全无关的描述仍要如实返空**——不能为了"有结果"硬凑。

    （早先这里用的是 "订单服务响应超时"，断言它匹配不上。那个断言在 matcher 修复后
    不再成立——中文现在能命中了，见 ``test_chinese_two_char_keyword_matches``。）
    """
    r = gq.infer_candidates(graph, problem="zzzz qqqq 完全无关的描述")
    assert r["candidate_apps"] == []
    assert "请勿编造服务名" in r["coverage_note"]


# ======================================================================
# E3：影响面是**独立**的一轴
# ======================================================================
def test_impact_is_independent_of_confidence(graph) -> None:
    """order-service 靠文本命中（medium）但带 tier1 且 critical（impact=high）。

    两轴分开正是为了不让"重要"冒充"可能"——若把 tier1 折进 confidence，
    这个候选会看起来和症状服务（high）一样可信。
    """
    r = gq.infer_candidates(graph, problem="order-service 响应超时")
    cand = _by_service(r)["order-service"]
    assert cand["confidence"] == "medium"    # 文本命中，够不到 high
    assert cand["impact"] == "high"          # 但影响面确实大


def test_impact_does_not_create_candidates(graph) -> None:
    """tier1 服务如果没有任何证据指向它，就不该出现在候选里。"""
    r = gq.infer_candidates(graph, problem="完全无关的描述")
    assert "payment-service" not in _by_service(r)   # payment 是 tier1，但无证据


def test_confidence_sorts_before_impact(graph) -> None:
    """排序：证据强度优先于影响面。"""
    r = gq.infer_candidates(
        graph, problem="order-service", services=["audit-service"], max_hops=1
    )
    ranks = {c["service"]: c["rank"] for c in r["candidate_apps"]}
    # audit-service 是症状服务（high），应排在 order-service（文本命中 low / 邻居 medium）前
    assert ranks["audit-service"] < ranks["order-service"]


# ======================================================================
# 诚实空结果
# ======================================================================
def test_no_evidence_yields_empty_and_says_so(graph) -> None:
    r = gq.infer_candidates(graph, problem="zzzz 完全不存在的服务")
    assert r["candidate_apps"] == []
    assert r["degraded"] is True
    assert r["incident_data_available"] is False
    assert r["coverage_note"]
    assert "请勿编造服务名" in r["coverage_note"]


def test_every_candidate_has_non_empty_reasons(graph) -> None:
    """候选必须能追溯到具体事实——没有理由的候选不该存在。"""
    r = gq.infer_candidates(
        graph, problem="order-service 超时", services=["warranty-service"], max_hops=2
    )
    assert r["candidate_apps"]
    for cand in r["candidate_apps"]:
        assert cand["reasons"], f"{cand['service']} 没有理由"


def test_empty_problem_rejected(graph) -> None:
    with pytest.raises(AppError):
        gq.infer_candidates(graph, problem="   ")


def test_limit_is_respected(graph) -> None:
    r = gq.infer_candidates(graph, problem="x", services=["gateway-service"], max_hops=3, limit=2)
    assert len(r["candidate_apps"]) <= 2


def test_ranks_are_contiguous_from_one(graph) -> None:
    r = gq.infer_candidates(graph, problem="order-service", services=["warranty-service"])
    ranks = [c["rank"] for c in r["candidate_apps"]]
    assert ranks == list(range(1, len(ranks) + 1))
