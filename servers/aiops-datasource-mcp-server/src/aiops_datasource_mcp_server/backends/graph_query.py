"""实体图谱的查询与候选推断（**纯函数，无 IO**）。

对标参考 ontology 的四个筛选**维度**——NODE TYPE / PORTFOLIO / KEY ATTRIBUTES /
EDGE TYPE——参数名与维度一一对应，调用方（agent 或将来的界面）可以把筛选栏直接翻译成
一次调用。

## 两个刻意的设计

**计数与 facets 每次现算，从不存储。** 这同时满足两条要求：派生指标不进静态文件
（见 ``entity_graph``）；空骨架不伪装成真实数据——空类型就是 ``[]`` 渲染出的 ``count: 0``，
不需要任何 ``populated: false`` 之类的存储态。

**未知筛选值 fail-closed。** 拼错的类型名必须报错并列合法值，否则 agent 会用一个不存在的
类型拿到全部结果还以为筛过了。而且要把"拼错"和"没数据"分开报——后者换个拼写重试一万次
也没用。
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from aiops_datasource_mcp_server.backends.entity_graph import EntityGraph
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

#: 候选服务推断沿这些边扩展（``incident_cluster_app`` 等在有事件数据时才有效果）。
_EXPANSION_EDGE_TYPES = ("calls", "incident_cluster_app", "change_cluster_app")

#: app 上参与子串匹配的**属性**字段。
#: ⚠️ 服务名不在 attributes 里，它是节点的 ``name`` 字段——见 ``_match_node``。
_TEXT_MATCH_FIELDS = ("owner", "namespace", "tech")

#: 纯 ASCII 值参与子串匹配的最小长度——防止 `tech="Go"` 匹配 "logs are **go**ing crazy"。
_MIN_TEXT_MATCH_LEN = 3

#: 含 CJK 的值只需 2 字符（见 ``_min_match_len``）。
_MIN_CJK_MATCH_LEN = 2

_CJK_RE = re.compile(r"[一-鿿]")

#: 字段值的分隔符——用于把 ``tech: "Java / Spring Boot"`` 切成可独立匹配的词元。
_FIELD_SPLIT_RE = re.compile(r"\s*[/,;、，]\s*")


def _field_tokens(value: str) -> list[str]:
    """把字段值切成**可独立匹配**的词元。

    ``tech: "Java / Spring Boot"`` **整串**拿去子串匹配是没用的——工单里不会出现
    ``"Java / Spring Boot"`` 这一整串，所以「升级 Java 版本」永远匹配不上。
    必须按分隔符切成 ``["Java", "Spring Boot"]`` 逐词元匹配。
    """
    return [t.strip() for t in _FIELD_SPLIT_RE.split(value) if t.strip()]


def _min_match_len(value: str) -> int:
    """该值参与子串匹配所需的最小长度——**按字符类型区分**。

    汉字信息密度高，「订单」「支付」「库存」「退款」这类**双字词**是最常见也最有信息量
    的单位；统一要求 3 会把它们**全部误杀**（实测本 CMDB 的中文关键词有 33 个是双字的，
    占绝大多数）。纯 ASCII 值仍要求 3——那是为了挡 `tech="Go"` 这类短值在英文里乱命中。
    """
    return _MIN_CJK_MATCH_LEN if _CJK_RE.search(value) else _MIN_TEXT_MATCH_LEN

#: 业务层节点命中后，**下钻得到的候选**的初始置信（§3.4 层级衰减：命中层越细越可信）。
#: 全表见 ``infer_candidates`` 的档位说明——``high`` 只留给**直接证据**
#: （症状服务 / 工单 cmdb_ci 指定），业务词命中够不到那一档。
_LAYER_CONFIDENCE = {
    "domain": "medium",     # 粒度最细，误召最少
    "portfolio": "low",
    "journey": "low",
    "enterprise": "low",    # 过宽：下钻可能覆盖全量，不足以单独作数（reasons 里有说明）
}

#: business 层边的「向下」方向：{边类型: (父类型, 子类型)}。
#: 下钻**必须**按它判定哪端是子级——业务边是无向的，随边乱走会往上跑到父级。
_BUSINESS_DOWN = {
    "enterprise_journey": ("enterprise", "journey"),
    "journey_link": ("journey", "portfolio"),
    "portfolio_link": ("portfolio", "app"),
    "domain_link": ("domain", "app"),
}

_CONFIDENCE_ORDER = ("low", "medium", "high")
#: 影响面档位。与 confidence **分属两个轴**：前者是"如果有关，影响多大"，
#: 后者是"它有关的证据有多强"。排序时 confidence 优先于 impact。
_IMPACT_ORDER = ("low", "medium", "high")


# ======================================================================
# 派生视图
# ======================================================================
def _degrees(graph: EntityGraph) -> dict[str, int]:
    """每个节点的度数（全图范围内，入+出）。对应于参考界面的 BLAST RADIUS 徽章。"""
    deg = dict.fromkeys(graph.nodes, 0)
    for edge in graph.edges:
        deg[edge["from"]] = deg.get(edge["from"], 0) + 1
        deg[edge["to"]] = deg.get(edge["to"], 0) + 1
    return deg


def compute_counts(graph: EntityGraph) -> dict[str, dict[str, int]]:
    return {
        "node_types": {
            t.key: len(graph.nodes_by_type.get(t.key, [])) for t in graph.ontology.node_types
        },
        "edge_types": {
            e.key: sum(1 for x in graph.edges if x["type"] == e.key)
            for e in graph.ontology.edge_types
        },
    }


def compute_facets(graph: EntityGraph) -> dict[str, list[dict]]:
    """四个维度的可用值清单（带命中计数）——**每次现算，从不存储**。"""
    counts = compute_counts(graph)

    node_types = [
        {"key": t.key, "label": t.label, "label_zh": t.label_zh, "view": t.view,
         "count": counts["node_types"][t.key]}
        for t in graph.ontology.node_types
    ]

    portfolios = []
    for nid in graph.nodes_by_type.get("portfolio", []):
        node = graph.nodes[nid]
        portfolios.append({
            "key": node["name"], "label": node["display_name"] or node["name"],
            "count": sum(
                1 for e in graph.edges
                if e["type"] == "portfolio_link" and nid in (e["from"], e["to"])
            ),
        })

    tag_counts: dict[str, int] = dict.fromkeys(graph.key_attribute_keys, 0)
    for node in graph.nodes.values():
        for tag in node["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    key_attributes = [
        {"key": a.key, "label": a.label, "label_zh": a.label_zh,
         "count": tag_counts.get(a.key, 0)}
        for a in graph.ontology.key_attributes
    ]

    edge_types = [
        {"key": e.key, "label": e.label, "label_zh": e.label_zh,
         "directed": e.directed, "count": counts["edge_types"][e.key]}
        for e in graph.ontology.edge_types
    ]

    return {
        "node_types": node_types,
        "portfolios": portfolios,
        "key_attributes": key_attributes,
        "edge_types": edge_types,
    }


def unavailable_dimensions(facets: dict[str, list[dict]]) -> list[dict]:
    """整维为空（该维度下**所有**取值计数为 0）时如实报出来。

    注意与"某个取值为 0"区分：``holiday_critical`` 计数为 0，但它是**声明过的**
    合法取值，过滤它返回空是正常语义，不算维度不可用。
    """
    out = []
    for dim, entries in facets.items():
        if not entries:
            out.append({
                "dimension": dim,
                "reason": "本 CMDB 该维度没有任何取值（数据未录入，不是查询问题）",
            })
        elif all(e.get("count", 0) == 0 for e in entries):
            out.append({
                "dimension": dim,
                "reason": f"本 CMDB 的 {dim} 维度全部取值为空（数据未录入）",
            })
    return out


# ======================================================================
# 筛选
# ======================================================================
def _validate_values(values, vocabulary, dimension: str, *, empty_hint: str) -> None:
    if not values:
        return
    if not vocabulary:
        # 「拼错」与「没数据」必须分开——否则 agent 会无限换拼写重试
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{dimension} 维度当前没有任何取值：{empty_hint}。"
            f"这不是拼写问题，是数据未录入——请勿反复更换写法重试。",
            {"dimension": dimension, "requested": list(values)},
        )
    unknown = [v for v in values if v not in vocabulary]
    if unknown:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{dimension} 含未知取值 {unknown}；合法取值：{sorted(vocabulary)}",
            {"dimension": dimension, "unknown": unknown, "valid": sorted(vocabulary)},
        )


def _portfolio_members(graph: EntityGraph) -> dict[str, set[str]]:
    """portfolio 名 → 该 portfolio 关联的节点 id 集合（含 portfolio 自身）。"""
    out: dict[str, set[str]] = {}
    for nid in graph.nodes_by_type.get("portfolio", []):
        out[graph.nodes[nid]["name"]] = {nid}
    for edge in graph.edges:
        if edge["type"] != "portfolio_link":
            continue
        for a, b in ((edge["from"], edge["to"]), (edge["to"], edge["from"])):
            if (
                a in graph.nodes
                and b in graph.nodes
                and graph.nodes[a]["type"] == "portfolio"
            ):
                out.setdefault(graph.nodes[a]["name"], {a}).add(b)
    return out


def _adjacency(graph: EntityGraph, edge_types: set[str] | None) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {nid: set() for nid in graph.nodes}
    for edge in graph.edges:
        if edge_types is not None and edge["type"] not in edge_types:
            continue
        adj[edge["from"]].add(edge["to"])
        adj[edge["to"]].add(edge["from"])   # 探索性遍历按无向走
    return adj


def _neighborhood(
    graph: EntityGraph, origin: str, hops: int, edge_types: set[str] | None
) -> dict[str, int]:
    if origin not in graph.nodes:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"未知节点 id {origin!r}；格式为 <type>:<name>，如 app:order-service",
            {"node_id": origin},
        )
    adj = _adjacency(graph, edge_types)
    dist = {origin: 0}
    queue: deque[str] = deque([origin])
    while queue:
        cur = queue.popleft()
        if dist[cur] >= hops:
            continue
        for nb in adj[cur]:
            if nb not in dist:
                dist[nb] = dist[cur] + 1
                queue.append(nb)
    return dist


def _drill_to_apps(graph: EntityGraph, start: str, max_depth: int = 4) -> set[str]:
    """从业务层节点沿**向下**的业务边走到 app（§3.4 的「沿图下钻」）。

    这一步是"问题域 → 方案域"的落点：问题描述常常根本不含服务名，但它含**业务词**
    （「打印结账单没反应」）。命中 journey/portfolio/domain 之后，靠图关系才能落到具体服务。
    """
    if graph.nodes[start]["type"] == "app":
        return {start}

    # 先按声明方向建出"父 → 子"的邻接表（无向边可能两个方向都写了）
    children: dict[str, set[str]] = {}
    for edge in graph.edges:
        pair = _BUSINESS_DOWN.get(edge["type"])
        if pair is None:
            continue
        parent_t, child_t = pair
        f_type, t_type = graph.nodes[edge["from"]]["type"], graph.nodes[edge["to"]]["type"]
        if f_type == parent_t and t_type == child_t:
            children.setdefault(edge["from"], set()).add(edge["to"])
        elif f_type == child_t and t_type == parent_t:
            children.setdefault(edge["to"], set()).add(edge["from"])

    out: set[str] = set()
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        cur, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for nb in children.get(cur, ()):
            if nb in seen:
                continue
            seen.add(nb)
            if graph.nodes[nb]["type"] == "app":
                out.add(nb)
            queue.append((nb, depth + 1))
    return out


def _match_node(node: dict, haystack: str) -> tuple[list[str], bool]:
    """一个节点对问题描述的命中项（子串包含，**不分词、不做语义**）。

    返回 ``(命中项, 是否识别性命中)``。这个区分决定了置信度——**两种命中的证据强度
    差一档**：

    - **识别性命中**：命中 ``keywords`` 或节点自己的 ``name``。这两个都是"**说明它是谁**"
      的词（`订单`、`order-service`）。
    - **非识别性命中**：只命中 ``owner`` / ``namespace`` / ``tech``。这些是**共享属性**
      ——6 个服务都跑 Java、3 个都在 order namespace。"命中"只说明它在这个集合里，
      不说明它与故障有关，所以证据弱一档（见 ``infer_candidates`` 里给 low 的分支）。

    ⚠️ 服务名取自 ``node["name"]``，**不是** ``attributes["name"]``——后者根本不存在。
    早先的实现读 attributes，导致**按服务名匹配从未生效过**。
    """
    matched: list[str] = []
    for kw in node.get("keywords", []):
        if len(kw) >= _min_match_len(kw) and kw.lower() in haystack:
            matched.append(f"keywords:{kw}")
    if node["type"] != "app":
        return matched, bool(matched)
    name = node["name"]
    if len(name) >= _min_match_len(name) and name.lower() in haystack:
        matched.append(f"name={name}")
    identity = bool(matched)
    for field in _TEXT_MATCH_FIELDS:
        value = node["attributes"].get(field) or ""
        for token in _field_tokens(value):
            if len(token) >= _min_match_len(token) and token.lower() in haystack:
                matched.append(f"{field}={token}")
    return matched, identity


def _bump(confidence: str, steps: int = 1) -> str:
    i = min(_CONFIDENCE_ORDER.index(confidence) + steps, len(_CONFIDENCE_ORDER) - 1)
    return _CONFIDENCE_ORDER[i]


def query_graph(
    graph: EntityGraph,
    *,
    node_types: list[str] | None = None,
    portfolios: list[str] | None = None,
    key_attributes: list[str] | None = None,
    edge_types: list[str] | None = None,
    node_id: str | None = None,
    hops: int = 1,
    include_facets: bool = True,
) -> dict:
    """按四个维度筛选 + 可选聚焦展开。"""
    facets = compute_facets(graph)
    _validate_values(
        node_types, {t.key for t in graph.ontology.node_types}, "node_types",
        empty_hint="本文件的 ontology 未声明任何节点类型",
    )
    _validate_values(
        edge_types, {e.key for e in graph.ontology.edge_types}, "edge_types",
        empty_hint="本文件的 ontology 未声明任何边类型",
    )
    _validate_values(
        portfolios, {p["key"] for p in facets["portfolios"]}, "portfolios",
        empty_hint="本 CMDB 未收录 Portfolio 节点",
    )
    _validate_values(
        key_attributes, set(graph.key_attribute_keys), "key_attributes",
        empty_hint="本 CMDB 未声明任何静态横切标签",
    )

    keep: set[str] = set(graph.nodes)
    if node_types:
        keep &= {nid for nid in graph.nodes if graph.nodes[nid]["type"] in set(node_types)}
    if key_attributes:
        want = set(key_attributes)
        keep &= {nid for nid in graph.nodes if set(graph.nodes[nid]["tags"]) & want}
    if portfolios:
        members: set[str] = set()
        pm = _portfolio_members(graph)
        for name in portfolios:
            members |= pm.get(name, set())
        keep &= members

    focused = None
    if node_id is not None:
        focused = _neighborhood(
            graph, node_id, hops, set(edge_types) if edge_types else None
        )
        keep &= set(focused)

    kept_edges = [
        e for e in graph.edges
        if e["from"] in keep and e["to"] in keep
        and (not edge_types or e["type"] in set(edge_types))
    ]

    deg = _degrees(graph)
    nodes_out = []
    for nid in sorted(keep, key=lambda n: (graph.nodes[n]["type"], graph.nodes[n]["name"])):
        node = graph.nodes[nid]
        item = {
            "id": nid,
            "type": node["type"],
            "name": node["name"],
            "display_name": node["display_name"],
            "attributes": node["attributes"],
            "tags": node["tags"],
            "refs": node["refs"],
            "degree": deg.get(nid, 0),
        }
        if focused is not None:
            item["distance"] = focused.get(nid)
        nodes_out.append(item)

    result: dict[str, Any] = {
        "schema_version": graph.schema_version,
        "graph": {
            "path": graph.path,
            "node_count": graph.node_count,
            "edge_count": len(graph.edges),
        },
        "counts": compute_counts(graph),
        "filters": {
            "node_types": node_types or [],
            "portfolios": portfolios or [],
            "key_attributes": key_attributes or [],
            "edge_types": edge_types or [],
            "node_id": node_id,
            "hops": hops if node_id else None,
        },
        "matched": {"nodes": len(nodes_out), "edges": len(kept_edges)},
        "derived_metrics_declared": [
            {"key": m.key, "label": m.label, "window_months": m.window_months, "available": False}
            for m in graph.ontology.derived_metrics
        ],
        "nodes": nodes_out,
        "edges": kept_edges,
        "summary": _query_summary(nodes_out, kept_edges, graph),
    }
    if include_facets:
        result["facets"] = facets
        result["unavailable_dimensions"] = unavailable_dimensions(facets)
    return result


def _query_summary(nodes_out: list[dict], edges: list[dict], graph: EntityGraph) -> str:
    by_type: dict[str, int] = {}
    for n in nodes_out:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    detail = "、".join(f"{k} {v}" for k, v in sorted(by_type.items())) or "无"
    return f"命中 {len(nodes_out)} 个节点（{detail}）与 {len(edges)} 条边"


# ======================================================================
# 候选服务推断
# ======================================================================
def infer_candidates(
    graph: EntityGraph,
    *,
    problem: str,
    services: list[str] | None = None,
    namespaces: list[str] | None = None,
    max_hops: int = 1,
    limit: int = 10,
) -> dict:
    """问题描述 → 候选应用集合。

    ## 三档证据（排序后的强度）

    **E1 症状服务（主）**：调用方从日志/链路已确认的服务名——这是比文本匹配强得多的
    证据，强烈建议传。命中者置信 high，沿依赖边扩展出来的邻居置信 medium
    （``upstream`` 是爆炸半径、``downstream`` 是可能的上游根因，两者都是合法候选，
    但理由不同）。

    **E2 分层关键词匹配**：**子串包含**（不做空白分词——中文没有词边界，
    ``"订单服务响应超时".split()`` 只会得到一个没用的整串），扫**所有节点类型**，
    不只 app。问题描述常常根本不含服务名（「打印结账单没反应」里一个都没有），
    但它含**业务词**——命中业务层节点后沿业务边**下钻**到 app（§3.4 的映射）。

    **置信度按证据类型分档**，不是按命中位置：

    | 档 | 来源 |
    |---|---|
    | ``high`` | **直接证据**：症状服务（调用方从日志确认）、工单 ``cmdb_ci`` 指定 |
    | ``medium`` | app 的**识别性命中**（name / keywords）、``domain`` 下钻、拓扑邻居 |
    | ``low`` | ``portfolio``/``journey``/``enterprise`` 下钻、只命中共享属性 |

    ``high`` 只留给直接证据——业务词命中再准也只是"工单提到了它"，不该与
    "日志里确认它在报错"同级。

    多条独立路径命中同一 app 会**升一档**（见 E4 交叉验证）。

    **E3 标签与 criticality 只影响 ``impact``，不创造候选、也不改 ``confidence``**：
    后者问"它有关的证据有多强"，前者问"如果有关影响多大"。两个轴分开，调用方才能
    自己决定是"先查最可能的"还是"先查最要紧的"。
    """
    if not problem.strip():
        raise AppError(ErrorCode.INVALID_REQUEST, "problem 不能为空")

    haystack = problem.lower()
    deg = _degrees(graph)
    apps: dict[str, dict] = {}   # node_id → {confidence, reasons, distance}
    evidence_used: list[str] = []
    unresolved: list[str] = []
    have_incident_data = bool(graph.nodes_by_type.get("incident")) or bool(
        graph.nodes_by_type.get("change")
    )

    def _touch(
        nid: str,
        confidence: str,
        reason: str,
        distance: int | None,
        source: str | None = None,
    ) -> None:
        cur = apps.setdefault(
            nid,
            {"confidence": confidence, "impact": "low", "reasons": [],
             "distance": distance, "sources": set()},
        )
        if _CONFIDENCE_ORDER.index(confidence) > _CONFIDENCE_ORDER.index(cur["confidence"]):
            cur["confidence"] = confidence
        if reason not in cur["reasons"]:
            cur["reasons"].append(reason)
        if distance is not None and (cur["distance"] is None or distance < cur["distance"]):
            cur["distance"] = distance
        if source is not None:
            # 记录"是哪几条独立证据把它推出来的"——§3.4 的交叉验证素材
            cur["sources"].add(source)

    # ---- E1：症状服务 + 拓扑扩展 ----
    name_to_id = {
        graph.nodes[nid]["name"]: nid for nid in graph.nodes_by_type.get("app", [])
    }
    if services:
        evidence_used.append("symptom_services")
        seeded = False
        for svc in services:
            nid = name_to_id.get(svc)
            if nid is None:
                unresolved.append(svc)
                continue
            seeded = True
            _touch(nid, "high", f"症状服务（调用方已确认）：{svc}", 0)
        if seeded:
            evidence_used.append("topology_expansion")
            adj = _adjacency(graph, set(_EXPANSION_EDGE_TYPES))
            for svc in services:
                origin = name_to_id.get(svc)
                if origin is None or max_hops <= 0:
                    continue
                dist = {origin: 0}
                queue: deque[str] = deque([origin])
                while queue:
                    cur = queue.popleft()
                    if dist[cur] >= max_hops:
                        continue
                    for nb in adj[cur]:
                        if nb not in dist:
                            dist[nb] = dist[cur] + 1
                            queue.append(nb)
                for nid, d in dist.items():
                    if nid == origin or graph.nodes[nid]["type"] != "app":
                        continue
                    _touch(nid, "medium", f"与症状服务 {svc} 相距 {d} 跳依赖关系", d)

    # ---- E1b：事件节点匹配（仅当覆盖层提供了 Incident / Change）----
    #
    # 这是参考 ontology 里那条「问题 → 事件 → 应用」的路径：先把问题描述对上事件节点，
    # 再沿 incident_cluster_app / change_cluster_app 落到 App。没有事件数据时整段跳过，
    # 且**不需要改代码**——数据到位那天它自己就通了。
    #
    # 置信度给 medium 而非 high：问题→事件的匹配仍是文本比对（弱证据）；
    # 强的是"事件→应用"这条边上本来就声明好的落点关系。
    if have_incident_data:
        event_types = ("incident", "change")
        event_ids = [
            nid for t in event_types for nid in graph.nodes_by_type.get(t, [])
        ]
        event_adj = _adjacency(graph, {"incident_cluster_app", "change_cluster_app"})
        for nid in event_ids:
            node = graph.nodes[nid]
            fields = [
                node["name"],
                node.get("display_name") or "",
                node.get("notes") or "",
                *[v for v in node["attributes"].values() if isinstance(v, str)],
            ]
            if not any(
                len(f) >= _MIN_TEXT_MATCH_LEN and f.lower() in haystack for f in fields
            ):
                continue
            if "incident_match" not in evidence_used:
                evidence_used.append("incident_match")
            for nb in event_adj.get(nid, ()):
                if graph.nodes[nb]["type"] == "app":
                    _touch(
                        nb, "medium",
                        f"事件 {node['name']}（{node['type']}）的落点应用",
                        None,
                    )

    if namespaces:
        want = set(namespaces)
        hits = [
            nid for nid in graph.nodes_by_type.get("app", [])
            if graph.nodes[nid]["attributes"].get("namespace") in want
        ]
        if hits:
            evidence_used.append("namespace_match")
        for nid in hits:
            ns = graph.nodes[nid]["attributes"]["namespace"]
            _touch(nid, "low", f"namespace 命中：{ns}", None)

    # ---- E2：分层关键词匹配（子串包含，**不分词、不做语义**）----
    #
    # 扫**所有节点类型**，不只 app。这是 §3.4「问题域分层 ↔ 方案域分层」的落点：
    # 问题描述常常根本不含服务名（「打印结账单没反应」里一个服务名都没有），但它含
    # **业务词**。命中业务层节点后沿业务边**下钻**到 app，才是完整的映射。
    #
    # 命中层的粗细决定初始置信（§3.4 层级衰减）——命中 domain 比命中 enterprise 可信得多，
    # 因为后者的下钻可能覆盖全量服务，等于没缩。
    text_hits = 0
    for nid, node in graph.nodes.items():
        matched, identity = _match_node(node, haystack)
        if not matched:
            continue
        text_hits += 1
        shown = "、".join(matched[:3])
        if node["type"] == "app":
            # 识别性命中（名字/关键词）→ medium；只命中共享属性（tech/owner/namespace）→ low
            _touch(
                nid, "medium" if identity else "low",
                f"问题描述命中 {shown}", None, source=nid,
            )
            continue
        layer_conf = _LAYER_CONFIDENCE.get(node["type"], "low")
        note = "（**过宽**，不足以单独作数）" if node["type"] == "enterprise" else ""
        for app_nid in _drill_to_apps(graph, nid):
            _touch(
                app_nid, layer_conf,
                f"{node['type']}「{node['display_name'] or node['name']}」命中 → 下钻"
                f"（{shown}）{note}",
                None, source=nid,
            )
    if text_hits:
        evidence_used.append("problem_text_match")

    # ---- E3：标签 / criticality → **独立的影响面轴**，不并进 confidence ----
    #
    # 这里刻意**不**用它提升 confidence。原因是两个轴问的不是同一件事：
    #   confidence —— 「这个服务与故障有关」的证据有多强（症状服务 / 拓扑邻居 / 文本命中）
    #   impact     —— 「如果它确实有关，影响面多大」（tier1 / criticality）
    # 把 tier1 折进 confidence，会让一个仅凭拓扑相邻的候选拿到与"调用方已确认的症状服务"
    # 同等的置信度标签——那是把重要性冒充成可能性，正是本模块要避免的那类假信号。
    for nid, entry in apps.items():
        node = graph.nodes[nid]
        attrs = node["attributes"]
        notes = []
        impact = "low"
        if node["tags"]:
            notes.append(f"静态标签：{'、'.join(node['tags'])}")
            impact = "high"
        crit = attrs.get("criticality")
        if crit == "critical":
            notes.append("criticality=critical")
            impact = "high"
        elif crit == "high" and impact != "high":
            notes.append("criticality=high")
            impact = "medium"
        entry["impact"] = impact
        entry["reasons"].extend(notes)

    # ---- E4：交叉验证——**多条独立路径命中同一 app，证据强于单条** ----
    #
    # 这是分层映射最大的增益（§3.4）。与 E3 的区别值得说清：
    # E3（标签/criticality）**不**碰 confidence——那是"重要性"冒充"可能性"；
    # 这里碰，因为"被 3 条独立证据指向"本来就是**可能性**的正面证据，同一个轴。
    for entry in apps.values():
        n_paths = len(entry["sources"])
        layers = sorted({graph.nodes[s]["type"] for s in entry["sources"]})
        entry["hit_paths"] = n_paths
        entry["matched_layers"] = layers
        if n_paths >= 2:
            entry["confidence"] = _bump(entry["confidence"])
            entry["reasons"].append(f"{n_paths} 条独立路径交叉命中（{'、'.join(layers)}）")

    # 排序：先证据强度，再影响面，再距离——证据永远优先于重要性
    ranked = sorted(
        apps.items(),
        key=lambda kv: (
            -_CONFIDENCE_ORDER.index(kv[1]["confidence"]),
            -_IMPACT_ORDER.index(kv[1]["impact"]),
            kv[1]["distance"] if kv[1]["distance"] is not None else 99,
            kv[0],
        ),
    )[:limit]

    candidates = [
        {
            "rank": i,
            "service": graph.nodes[nid]["name"],
            "node_id": nid,
            "confidence": entry["confidence"],
            "impact": entry["impact"],
            "reasons": entry["reasons"],
            "hit_paths": entry["hit_paths"],
            "matched_layers": entry["matched_layers"],
            "distance_from_symptom": entry["distance"],
            "attributes": graph.nodes[nid]["attributes"],
            "tags": graph.nodes[nid]["tags"],
            "degree": deg.get(nid, 0),
        }
        for i, (nid, entry) in enumerate(ranked, start=1)
    ]

    degraded = not have_incident_data
    parts = []
    if degraded:
        parts.append(
            "本 CMDB 无 Incident / Change 记录（incident 与 change 节点为空），"
            "故未走「事件 → 应用」这条路径；候选来自症状服务与依赖拓扑扩展 + "
            "静态字段关键词匹配。"
        )
    else:
        parts.append("已纳入 Incident / Change 事件边。")
    parts.append(
        "文本匹配是**逐字子串**比对（不做分词）。注意其局限：中文组织标签"
        "（如 owner=「支付团队」）通常不会逐字出现在自由文本里，所以「支付超时」这类"
        "描述匹配不上——**没命中不等于无关**，只是匹配器认不出来。"
        "请优先用日志 / 链路确认症状服务后走 `services` 参数重试。"
        "**未命中任何服务时请勿编造服务名。**"
    )
    coverage = "".join(parts)

    return {
        "problem": problem,
        "incident_data_available": have_incident_data,
        "degraded": degraded,
        "evidence_used": evidence_used,
        "unresolved_services": unresolved,
        "candidate_apps": candidates,
        "coverage_note": coverage,
        "summary": (
            f"候选 {len(candidates)} 个"
            + (f"（另有 {len(unresolved)} 个服务名未收录）" if unresolved else "")
            + ("；无事件数据，已降级" if degraded else "")
        ),
    }
