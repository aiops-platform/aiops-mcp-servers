"""CMDB 后端：服务目录 + 依赖拓扑 + 仓库定位。

**数据是 mock**（服务目录与依赖关系内置在 `_SERVICES` / `_DEPENDS_ON`），但**接口是
生产的**——调用方（agent）看到的是"查 CMDB"，换成真实 CMDB 只需替换本模块的取数实现，
工具面与返回契约不变。

## 为什么放在 server 侧（而不是 agent 进程里）

- **租户隔离**：每个租户部署自己的 server（v5.3 P1），CMDB 随部署走；
  agent 进程里不再持有任何服务目录数据。
- **一处数据、多方消费**：拓扑查询（谁依赖我 / 我依赖谁）与仓库定位用同一份目录，
  不会出现"两个地方各记一份、逐渐漂移"。

## 拓扑查询语义

`get_service_topology(service, hops)` 从 `service` 出发做 **BFS**，默认 2 跳，返回:

- ``nodes``：可达服务，带 ``distance``（跳数）、``direction``（相对起点是
  ``upstream`` 调用方 / ``downstream`` 被调方）、``via``（从谁走过来）、
  以及该服务的目录信息（tier / owner / namespace / tech）。
- ``edges``：这些节点之间的边（带调用方向）。

**方向为什么要给**：故障诊断同时需要"谁会被我影响"（upstream，爆炸半径）与
"我依赖了谁"（downstream，可能的上游根因）——两者排查手法完全不同。

仓库定位（``locate_repo``）与目录是同源的；``DATASOURCE_REPO_ROOT`` 非空时返回
``file://`` 本地路径（testbed 联调），否则返回内置的远端仓库 URL。
"""
from __future__ import annotations

from collections import deque

from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# ----------------------------------------------------------------------
# Mock 服务目录（10 个服务，覆盖 edge / core / support 三层）
# ----------------------------------------------------------------------
# repo：仓库标识。远端模式下拼成 https://github.com/{org}/{repo}；
#       DATASOURCE_REPO_ROOT 非空时拼成 file://{root}/{repo}（testbed 本地目录名）。
_SERVICES: dict[str, dict] = {
    "gateway-service": {
        "namespace": "order", "owner": "网关团队", "tier": "edge",
        "tech": "Spring Cloud Gateway", "repo": "aiops-test-gateway-service",
        "runtime": "k8s", "criticality": "high",
    },
    "order-service": {
        "namespace": "order", "owner": "交易履约", "tier": "core",
        "tech": "Java / Spring Boot", "repo": "aiops-test-order-service",
        "runtime": "k8s", "criticality": "critical",
    },
    "warranty-service": {
        "namespace": "order", "owner": "售后保障", "tier": "core",
        "tech": "Java / Spring Boot", "repo": "aiops-test-warranty-service",
        "runtime": "k8s", "criticality": "high",
    },
    "payment-service": {
        "namespace": "payment", "owner": "支付团队", "tier": "core",
        "tech": "Java / Spring Boot", "repo": "payment-service",
        "runtime": "k8s", "criticality": "critical",
    },
    "inventory-service": {
        "namespace": "inventory", "owner": "库存团队", "tier": "core",
        "tech": "Go", "repo": "inventory-service",
        "runtime": "k8s", "criticality": "high",
    },
    "pricing-service": {
        "namespace": "order", "owner": "定价团队", "tier": "core",
        "tech": "Java / Spring Boot", "repo": "pricing-service",
        "runtime": "k8s", "criticality": "medium",
    },
    "logistics-service": {
        "namespace": "logistics", "owner": "履约调度", "tier": "support",
        "tech": "Go", "repo": "logistics-service",
        "runtime": "k8s", "criticality": "medium",
    },
    "notification-service": {
        "namespace": "common", "owner": "平台基础", "tier": "support",
        "tech": "Python / FastAPI", "repo": "notification-service",
        "runtime": "k8s", "criticality": "low",
    },
    "user-service": {
        "namespace": "account", "owner": "用户中心", "tier": "core",
        "tech": "Java / Spring Boot", "repo": "user-service",
        "runtime": "k8s", "criticality": "high",
    },
    "audit-service": {
        "namespace": "common", "owner": "安全合规", "tier": "support",
        "tech": "Go", "repo": "audit-service",
        "runtime": "k8s", "criticality": "low",
    },
}

# 依赖图：caller → [callee...]（同步 RPC 调用关系）
_DEPENDS_ON: dict[str, list[tuple[str, str]]] = {
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


def _downstream_of(service: str) -> dict[str, str]:
    """service **直接调用**的下游：{callee: relation}。"""
    return dict(_DEPENDS_ON.get(service, []))


def _upstream_of(service: str) -> dict[str, str]:
    """**直接调用** service 的上游：{caller: relation}。"""
    out: dict[str, str] = {}
    for caller, callees in _DEPENDS_ON.items():
        for callee, relation in callees:
            if callee == service:
                out.setdefault(caller, relation)
    return out


def _info(service: str) -> dict:
    """目录信息（未知服务返回最小占位，不抛——拓扑里可能出现目录未收录的节点）。"""
    meta = _SERVICES.get(service, {})
    return {
        "service": service,
        "tier": meta.get("tier", "unknown"),
        "owner": meta.get("owner", "unknown"),
        "namespace": meta.get("namespace", "unknown"),
        "tech": meta.get("tech", ""),
        "criticality": meta.get("criticality", "unknown"),
    }


def _bfs(origin: str, hops: int, *, follow: str) -> dict[str, int]:
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
        for neighbor in step(cur):
            if neighbor in dist:
                continue
            dist[neighbor] = dist[cur] + 1
            seen[neighbor] = dist[neighbor]
            queue.append(neighbor)
    return seen


def _repo_url(service: str) -> str:
    """按配置拼仓库 URL：本地 root 优先（testbed），否则远端。"""
    settings = get_settings()
    repo = (_SERVICES.get(service) or {}).get("repo") or service
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

    known = service in _SERVICES

    # 两趟 BFS —— 方向必须**相对起点**定义，不能按遍历方向（从上游再往下走会到兄弟节点）：
    #   downstream：起点能**沿调用方向**到达的（我 → … → 它）
    #   upstream  ：能**沿调用方向**到达起点的（它 → … → 我）
    down = _bfs(service, hops, follow="down")
    up = _bfs(service, hops, follow="up")

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
        nodes.append({**_info(svc), "distance": distance, "direction": direction})
    nodes.sort(key=lambda n: (n["distance"], n["service"]))

    reached = {n["service"] for n in nodes}
    edges = [
        {"from": caller, "to": callee, "relation": rel}
        for caller, callees in _DEPENDS_ON.items()
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
    if service not in _SERVICES:
        return {
            "service": service, "found": False, "repo_url": "", "repo": "",
            "summary": f"CMDB 未收录服务 {service}（无法定位仓库）",
            "known_services": sorted(_SERVICES),
        }
    info = _info(service)
    repo = _SERVICES[service]["repo"]
    return {
        **info,
        "found": True,
        "repo": repo,
        "repo_url": _repo_url(service),
        "summary": f"{service} → {_repo_url(service)}（{info['owner']}，{info['tier']}）",
    }


def known_services() -> list[str]:
    """目录中的服务名（供工具描述与文档生成）。"""
    return sorted(_SERVICES)
