"""CMDB 后端：服务目录 + 依赖拓扑 + 仓库定位。

**数据来自实体文件**（``data/cmdb-entities.json``，随包分发；生产/多租户可用
``DATASOURCE_CMDB_PATH`` 指向挂载卷上授权编辑的文件）。本模块只负责**取数与工具面**
——schema 校验、派生索引都在 ``backends/entity_graph.py``。

**接口是生产的**：调用方（agent）看到的是"查 CMDB"，换数据只需改实体文件，工具面与
返回契约都不变。``_info`` / ``_bfs`` / ``_repo_url`` 等的实现保持历史形态，只把数据
来源从模块级字面量换成 ``EntityGraph`` 上**同形的派生索引**
（``services`` / ``depends_on`` / ``repo_by_app``）——这是"迁移无损"的落点。

## 为什么放在 server 侧（而不是 agent 进程里）

- **租户隔离**：每个租户部署自己的 server（v5.3 P1），CMDB 随部署走；
  agent 进程里不再持有任何服务目录数据。
- **一处数据、多方消费**：拓扑查询（谁依赖我 / 我依赖谁）与仓库定位用同一份目录，
  不会出现"两个地方各记一份、逐渐漂移"。

## 拓扑查询语义

`get_service_topology(service, hops)` 从 `service` 出发做 **BFS**，默认 2 跳，返回:

- ``nodes``：可达服务，带 ``distance``（跳数）、``direction``（相对起点是
  ``upstream`` 调用方 / ``downstream`` 被调方），以及该服务的目录信息
  （tier / owner / namespace / tech / criticality）。
- ``edges``：这些节点之间的边（带调用方向，且只含**两端都可达**的边）。

**方向为什么要给**：故障诊断同时需要"谁会被我影响"（upstream，爆炸半径）与
"我依赖了谁"（downstream，可能的上游根因）——两者排查手法完全不同。

仓库定位（``locate_repo``）与目录同源；``DATASOURCE_REPO_ROOT`` 非空时返回
``file://`` 本地路径（testbed 联调），否则返回 ``https://github.com/{org}/{repo}``。
"""
from __future__ import annotations

from collections import deque

from aiops_datasource_mcp_server.backends.entity_graph import EntityGraph, get_graph
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# ----------------------------------------------------------------------
# 目录 / 依赖图取数（数据来自实体文件，见 entity_graph.py）
# ----------------------------------------------------------------------
# 说明：这些 helper 都接受可选 ``graph``，让一次拓扑查询只解析一次配置/只 stat 一次
# 文件；不传则各自取当前图（缓存命中，行为一致）。


def _downstream_of(service: str, graph: EntityGraph | None = None) -> dict[str, str]:
    """service **直接调用**的下游：{callee: relation}。"""
    g = graph or get_graph()
    return dict(g.depends_on.get(service, []))


def _upstream_of(service: str, graph: EntityGraph | None = None) -> dict[str, str]:
    """**直接调用** service 的上游：{caller: relation}。"""
    g = graph or get_graph()
    out: dict[str, str] = {}
    for caller, callees in g.depends_on.items():
        for callee, relation in callees:
            if callee == service:
                out.setdefault(caller, relation)
    return out


def _info(service: str, graph: EntityGraph | None = None) -> dict:
    """目录信息（未知服务返回最小占位，不抛——拓扑里可能出现目录未收录的节点）。"""
    g = graph or get_graph()
    meta = g.services.get(service, {})
    return {
        "service": service,
        "tier": meta.get("tier", "unknown"),
        "owner": meta.get("owner", "unknown"),
        "namespace": meta.get("namespace", "unknown"),
        "tech": meta.get("tech", ""),
        "criticality": meta.get("criticality", "unknown"),
    }


def _bfs(
    origin: str, hops: int, *, follow: str, graph: EntityGraph | None = None
) -> dict[str, int]:
    """从 origin 做 BFS，返回 {可达服务: 跳数}（不含 origin）。

    ``follow="down"`` 沿调用方向（origin 调谁、它又调谁…）；
    ``follow="up"`` 沿反向（谁调 origin、谁又调它…）。
    """
    step = _downstream_of if follow == "down" else _upstream_of
    seen: dict[str, int] = {}
    queue: deque[str] = deque([origin])
    dist: dict[str, int] = {origin: 0}
    while queue:
        cur = queue.popleft()
        if dist[cur] >= hops:
            continue
        for neighbor in step(cur, graph):
            if neighbor in dist:
                continue
            dist[neighbor] = dist[cur] + 1
            seen[neighbor] = dist[neighbor]
            queue.append(neighbor)
    return seen


def _repo_url(service: str, graph: EntityGraph | None = None) -> str:
    """按配置拼仓库 URL：本地 root 优先（testbed），否则远端。"""
    settings = get_settings()
    g = graph or get_graph()
    repo = (g.services.get(service) or {}).get("repo") or service
    root = (settings.datasource_repo_root or "").strip().rstrip("/")
    if root:
        return f"file://{root}/{repo}"
    return f"https://github.com/{settings.datasource_repo_org}/{repo}"


# ======================================================================
# 工具实现
# ======================================================================
async def get_service_topology(service: str, hops: int = 2) -> dict:
    """从 ``service`` 出发查 N 跳内的相关服务。

    未知服务**不报错**（拓扑查询可能是探索性的，且目录可能不全）——返回空结果 +
    提示，让调用方自己决定；但会明确说明"该服务不在目录中"。
    """
    if hops < 0:
        raise AppError(ErrorCode.INVALID_REQUEST, f"hops 不能为负：{hops}")

    graph = get_graph()
    known = service in graph.services

    # 两趟 BFS —— 方向必须**相对起点**定义，不能按遍历方向（从上游再往下走会到兄弟节点）：
    #   downstream：起点能**沿调用方向**到达的（我 → … → 它）
    #   upstream  ：能**沿调用方向**到达起点的（它 → … → 我）
    down = _bfs(service, hops, follow="down", graph=graph)
    up = _bfs(service, hops, follow="up", graph=graph)

    nodes: list[dict] = []
    for svc in sorted(set(down) | set(up) | {service}):
        d_dn, d_up = down.get(svc), up.get(svc)
        if svc == service:
            direction, distance = "self", 0
        elif d_dn is not None and d_up is not None:
            direction, distance = "both", min(d_dn, d_up)  # 环上：两个方向都可达
        elif d_dn is not None:
            direction, distance = "downstream", d_dn
        elif d_up is not None:
            direction, distance = "upstream", d_up
        else:  # 不可达（svc 必来自 down/up/service 之一，此处纯防御）
            continue
        nodes.append({**_info(svc, graph), "distance": distance, "direction": direction})
    nodes.sort(key=lambda n: (n["distance"], n["service"]))

    reached = {n["service"] for n in nodes}
    edges = [
        {"from": caller, "to": callee, "relation": rel}
        for caller, callees in graph.depends_on.items()
        for callee, rel in callees
        if caller in reached and callee in reached
    ]

    if not known:
        summary = f"服务 {service} 不在 CMDB 目录中，无法给出拓扑（请确认服务名）"
    elif len(nodes) == 1:
        summary = f"服务 {service} 在 {hops} 跳内没有关联服务"
    else:
        summary = (
            f"{service} 的 {hops} 跳拓扑：{len(nodes) - 1} 个关联服务"
            f"（上游调用方 {len(up)}；下游被调方 {len(down)}）"
        )

    return {
        "service": service,
        "hops": hops,
        "known": known,
        "node_count": len(nodes),
        "nodes": nodes,
        "edges": edges,
        "upstream": sorted(up),      # 谁依赖我（爆炸半径）
        "downstream": sorted(down),  # 我依赖谁（可能的上游根因）
        "summary": summary,
    }


async def locate_repo(service: str) -> dict:
    """由服务名定位仓库（service → repo URL + 目录信息）。"""
    graph = get_graph()
    if service not in graph.services:
        return {
            "service": service, "found": False, "repo_url": "", "repo": "",
            "summary": f"CMDB 未收录服务 {service}（无法定位仓库）",
            "known_services": sorted(graph.services),
        }
    info = _info(service, graph)
    repo = graph.services[service]["repo"]
    return {
        **info,
        "found": True,
        "repo": repo,
        "repo_url": _repo_url(service, graph),
        "summary": f"{service} → {_repo_url(service, graph)}（{info['owner']}，{info['tier']}）",
    }


def known_services() -> list[str]:
    """目录中的服务名（供工具描述与文档生成）。"""
    return sorted(get_graph().services)
