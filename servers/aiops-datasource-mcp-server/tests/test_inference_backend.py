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
def test_text_match_on_service_name_is_low_confidence(graph) -> None:
    r = gq.infer_candidates(graph, problem="order-service 响应超时")
    cand = _by_service(r)["order-service"]
    assert cand["confidence"] == "low"
    assert any("name" in reason for reason in cand["reasons"])


def test_text_match_on_namespace(graph) -> None:
    r = gq.infer_candidates(graph, problem="payment 相关告警")
    assert "payment-service" in _by_service(r)


def test_short_field_values_do_not_false_positive(graph) -> None:
    """`tech="Go"` 只有两个字符——不设下限的话，任何含 "go" 的英文都会误命中。"""
    r = gq.infer_candidates(graph, problem="logs are going crazy")
    assert r["candidate_apps"] == []


def test_chinese_problem_text_honestly_misses(graph) -> None:
    """**记录一个真实局限，而不是把它藏起来**。

    文本匹配是逐字子串比对，而 owner 是「交易履约」「支付团队」这类组织标签——
    "订单服务响应超时" 里没有任何一个字段值逐字出现，所以匹配不上。
    正确做法是调用方先用日志/链路确认症状服务，再走 services 参数。
    """
    r = gq.infer_candidates(graph, problem="订单服务响应超时")
    assert r["candidate_apps"] == []
    assert "没命中不等于无关" in r["coverage_note"]


# ======================================================================
# E3：影响面是**独立**的一轴
# ======================================================================
def test_impact_is_independent_of_confidence(graph) -> None:
    """order-service 文本命中（证据弱）但带 tier1 且 critical（影响大）。

    两轴分开正是为了不让"重要"冒充"可能"——若把 tier1 折进 confidence，
    这个候选会看起来和症状服务一样可信。
    """
    r = gq.infer_candidates(graph, problem="order-service 响应超时")
    cand = _by_service(r)["order-service"]
    assert cand["confidence"] == "low"
    assert cand["impact"] == "high"


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
