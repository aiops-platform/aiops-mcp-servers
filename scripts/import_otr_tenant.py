#!/usr/bin/env python3
"""把 OTR 租户快照整合进 CMDB 实体文件（**以 otr.json 为准**）。

## 为什么需要一个脚本，而不是手改一次 JSON

`otr.json` 是前端 Constellation 的**渲染快照**，会随租户数据重新生成（`tools/tenant-gen/`）。
手改 `cmdb-entities.json` 的话，下次 OTR 数据一变，两边就永久漂移了。本脚本把整合规则
**写死成代码**，于是：

    重新导入 = 再跑一次；页面上的手工编辑 = 覆盖在产物之上

## 整合的是什么，不是什么

OTR 快照与 CMDB 的**语义层级不同**，这不是同一份数据的两个版本：

| | CMDB（整合前） | otr.json |
|---|---|---|
| app | 10 个 **k8s 微服务** | 50 个 **业务应用** |
| 调用拓扑（`calls`） | 13 条 | **0 条** |
| 仓库（`app_codebase`） | 10 条 | **0 条** |
| 业务层 | enterprise/journey 空 | 1 / 2 / 11 完整 |

**OTR 不提供调用拓扑与仓库的任何替代数据**，所以"以它为准"只能是"在它有话语权的地方
为准"：业务层、应用清单、业务分级、运营指标、标签。运行时那一层（`calls` /
`app_codebase` / 10 个 k8s 服务）**原样保留**——把它们删掉会让 `get_service_topology`
与 `locate_repo` 同时失去数据源，而 OTR 补不上。

## 桥接

保留下来的 10 个 k8s 服务在业务层原本无归属（只有 work-order / warranty 两个挂了）。
本脚本按语义给它们补 `portfolio_link`，让业务层与运行时层成为**一张连通图**。

⚠️ **这 10 条桥接边是人工判断，不是 OTR 提供的事实**。它写进 `metadata.derived_rules`
就是为了让人看得见、能在页面上改。

用法：

    python3 scripts/import_otr_tenant.py \
        --otr  ../service-intelligence-platform-ui/tools/tenant-data/otr.json \
        --cmdb servers/aiops-datasource-mcp-server/src/\
               aiops_datasource_mcp_server/data/cmdb-entities.json

    # 只看会改什么，不落盘
    python3 scripts/import_otr_tenant.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ----------------------------------------------------------------------
# 路径默认值（相对本仓库根）
# ----------------------------------------------------------------------
_SCRIPT = Path(__file__).resolve()
REPO_ROOT = _SCRIPT.parent.parent
DEFAULT_CMDB = (
    REPO_ROOT
    / "servers"
    / "aiops-datasource-mcp-server"
    / "src"
    / "aiops_datasource_mcp_server"
    / "data"
    / "cmdb-entities.json"
)
DEFAULT_OTR = (
    REPO_ROOT.parent
    / "service-intelligence-platform-ui"
    / "tools"
    / "tenant-data"
    / "otr.json"
)

#: 整合后实体文件声明的 schema 版本。主版本不变（新增字段是附加的），次版本 +1。
SCHEMA_VERSION = "1.1.0"

#: ``metadata.source`` 里标记"本脚本追加过"的锚点，用于幂等（见 build 末尾）。
_SOURCE_MARK = "OTR 租户整合"

#: OTR app 的 `portfolioId` → 保留下来的 k8s 服务该怎么挂。
#:
#: ⚠️ **人工判断，不是 OTR 数据**。依据是服务名/owner/按业务含义的最近邻。
#: 前两条（work-order→order-service、warranty→warranty-service）在整合前就已存在
#: 于实体文件里，这里只是把它们一并纳入生成过程，避免每次导入丢一次。
BRIDGE: dict[str, list[str]] = {
    "portfolio:work-order": ["order-service"],
    "portfolio:warranty": ["warranty-service"],
    "portfolio:accounting": ["payment-service", "audit-service"],
    "portfolio:parts": ["inventory-service"],
    "portfolio:offer-order": ["pricing-service", "gateway-service"],
    "portfolio:handover": ["logistics-service"],
    "portfolio:lead": ["user-service"],
    "portfolio:campaign": ["notification-service"],
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """kebab-case slug。实体文件的 id 形态是 ``<type>:<slug>``，slug 只允许
    ``[A-Za-z0-9._-]``。"""
    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"无法从 {text!r} 生成 slug")
    return slug


def derive_keywords(*parts: str | None) -> list[str]:
    """机械派生检索词——**去重、去空、保持顺序**。

    刻意**不含技术栈词**（Java / Kafka…）：按 design-v5.7 的记录，技术栈是**共享属性**
    （多个应用都跑 Java），混进 keywords 会让"命中 tech"被当成识别性命中。技术栈另有
    `tech` 字段承载。

    也刻意**不做中译**：OTR 的 label 多是产品缩写（DSR / VLMS / Xentry），硬翻译只会
    造出没人会说的词。真实的用户用语需要人工在编辑页面上补。
    """
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        for candidate in (part, part.lower(), part.replace(" ", "").lower()):
            candidate = candidate.strip()
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def _node(
    node_id: str,
    node_type: str,
    name: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
    attributes: dict | None = None,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    notes: str | None = None,
) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "display_name": display_name,
        "description": description,
        "attributes": attributes or {},
        "tags": tags or [],
        "keywords": keywords or [],
        "refs": {},
        "notes": notes,
    }


def _edge(edge_id: str, edge_type: str, src: str, dst: str, attributes: dict | None = None) -> dict:
    return {
        "id": edge_id,
        "type": edge_type,
        "from": src,
        "to": dst,
        "attributes": attributes or {},
    }


def build(otr_path: Path, cmdb_path: Path) -> dict:
    otr = json.loads(otr_path.read_text(encoding="utf-8"))
    old = json.loads(cmdb_path.read_text(encoding="utf-8"))

    tenant = otr["tenant"]
    snap = otr["snapshot"]

    # ---- 旧文件里要保留的东西 -------------------------------------------
    # 运行时层：10 个 k8s 服务 + codebase + calls 边 + app_codebase 边。
    # 这一层 OTR 补不上，原样搬运。
    old_apps = {n["id"]: n for n in old["nodes"]["app"]}
    k8s_app_ids = {
        nid
        for nid, n in old_apps.items()
        if n["attributes"].get("source", "kubernetes") == "kubernetes"
    }
    k8s_apps = [old_apps[nid] for nid in sorted(k8s_app_ids)]
    codebases = old["nodes"]["codebase"]
    runtime_edges = [
        e
        for e in old["edges"]
        if e["type"] in ("calls", "app_codebase")
    ]

    # 旧业务层里人工撰写的检索词——OTR 不提供 keywords，合并而非覆盖。
    # 这些词的价值见 design-v5.7：业务域的 keywords 要按「用户会怎么描述它出问题」写，
    # 而不是「这个业务域是什么」。丢掉它们等于把业务语言的召回一起丢掉。
    preserved_keywords: dict[str, list[str]] = {}
    for type_key in ("portfolio", "domain", "journey", "enterprise"):
        for n in old["nodes"].get(type_key, []):
            if n.get("keywords"):
                preserved_keywords[n["id"]] = list(n["keywords"])

    # ---- 业务层：enterprise / journey / portfolio -------------------------
    nodes: dict[str, list[dict]] = {t["key"]: [] for t in old["ontology"]["node_types"]}
    edges: list[dict] = []

    ent_label = tenant["client"]["displayName"]
    ent_id = f"enterprise:{slugify(ent_label)}"
    nodes["enterprise"].append(
        _node(
            ent_id,
            "enterprise",
            slugify(ent_label),
            display_name=ent_label,
            description=f"{ent_label} 的企业功能整体。",
            keywords=derive_keywords(ent_label, tenant.get("name")),
            notes=(
                "源：otr.json tenant.client.displayName / "
                f"snapshot.enterprise.label={snap['enterprise']['label']!r}"
            ),
        )
    )

    # journey：OTR 的 id 是 journey:1 / journey:2（无意义序号），改用 label 派生的语义 slug。
    journey_id_by_otr: dict[str, str] = {}
    for j in snap["journeys"]:
        slug = slugify(j["label"])
        jid = f"journey:{slug}"
        journey_id_by_otr[j["id"]] = jid
        nodes["journey"].append(
            _node(
                jid,
                "journey",
                slug,
                display_name=j["label"],
                description=f"{j['label']}（{j.get('capability', j['label'])}）。",
                attributes={"capability": j.get("capability") or j["label"]},
                keywords=derive_keywords(j["label"], j.get("capability")),
                notes=f"源：otr.json snapshot.journeys[{j['id']}]",
            )
        )
        edges.append(
            _edge(
                f"e:enterprise_journey:{ent_id.split(':')[1]}->{slug}",
                "enterprise_journey", ent_id, jid,
            )
        )

    # portfolio
    portfolio_of_app: dict[str, str] = {}
    for p in snap["portfolios"]:
        pid = p["id"]
        slug = pid.split(":", 1)[1]
        kw = derive_keywords(p["label"], slug)
        # 合并整合前人工撰写的词（如「工单」「退货」这类问题视角用语）
        for extra in preserved_keywords.get(pid, []):
            if extra not in kw:
                kw.append(extra)
        journey_otr = p.get("journeyId")
        nodes["portfolio"].append(
            _node(
                pid,
                "portfolio",
                slug,
                display_name=p["label"],
                description=f"{p['label']} 业务领域。",
                attributes={},
                keywords=kw,
                notes=f"源：otr.json snapshot.portfolios（业务分级 tier={p['tier']}）",
            )
        )
        if journey_otr and journey_otr in journey_id_by_otr:
            jid = journey_id_by_otr[journey_otr]
            edges.append(
                _edge(f"e:journey_link:{jid.split(':')[1]}->{slug}", "journey_link", jid, pid)
            )

    # ---- 应用层：50 个 OTR 业务应用 --------------------------------------
    tech_by_key = {
        a["appKey"]: " / ".join(a.get("techNames", [])) for a in snap.get("appTechnologies", [])
    }
    for a in snap["apps"]:
        pid = a["portfolioId"]
        portfolio_of_app[a["id"]] = pid
        tags: list[str] = []
        if a.get("holidayCritical"):
            tags.append("holiday_critical")
        if a.get("financeFreeze"):
            tags.append("finance_freeze")

        nodes["app"].append(
            _node(
                a["id"],
                "app",
                a["appKey"],
                display_name=a["label"],
                description=f"{a['label']}（{pid.split(':', 1)[1]}）",
                attributes={
                    "kind": "application",
                    "source": "otr-inventory",
                    "tech": tech_by_key.get(a["appKey"]) or None,
                    "business_tier": a["tier"],
                    "operational_status": a["operationalStatus"],
                    "incidents_total": a["incidentsTotal"],
                    "change_count": a["changeCount"],
                    "risk_score": a["riskScore"],
                },
                tags=tags,
                keywords=derive_keywords(a["label"], a["appKey"]),
                notes=f"源：otr.json snapshot.apps（appKey={a['appKey']}）",
            )
        )
        target = f"portfolio:{pid.split(':', 1)[1]}"
        # 边 id 用 app 的 **id slug**（保证全局唯一），不用 appKey——
        # appKey 在快照里经过 fanout 消歧，但不保证跨组合唯一。
        app_slug = a["id"].split(":", 1)[1]
        edges.append(
            _edge(
                f"e:portfolio_link:{target.split(':')[1]}->{app_slug}",
                "portfolio_link", target, a["id"],
            )
        )

    # ---- 应用层：保留下来的 10 个 k8s 服务 --------------------------------
    for n in k8s_apps:
        entry = json.loads(json.dumps(n))  # 深拷贝，避免污染入参
        entry["attributes"].setdefault("source", "kubernetes")
        nodes["app"].append(entry)
    nodes["codebase"] = codebases

    # ---- 桥接：把 10 个服务挂进 OTR 业务域 --------------------------------
    for portfolio_id, service_names in BRIDGE.items():
        for svc in service_names:
            app_id = f"app:{svc}"
            if app_id not in k8s_app_ids:
                raise SystemExit(f"桥接表引用了不存在的 k8s 服务：{app_id}")
            edges.append(
                _edge(
                    f"e:portfolio_link:{portfolio_id.split(':')[1]}->{svc}",
                    "portfolio_link",
                    portfolio_id,
                    app_id,
                )
            )

    # ---- 旧的手工业务节点：domain（OTR 无 domain 层，原样保留）-----------
    nodes["domain"] = old["nodes"]["domain"]
    for e in old["edges"]:
        if e["type"] == "domain_link":
            edges.append(e)

    # ---- 事件层：**刻意留空**（见下方 overlay 与 docs/cmdb-entities.md §6）----
    #
    # incident / change 属于**事件覆盖层**（DATASOURCE_INCIDENTS_PATH），不属于主文件。
    # 主文件放事件节点会有一个静默的副作用：`graph_query` 用「incident/change 桶是否非空」
    # 判断 `have_incident_data`，一旦非空就启用 E1b 事件匹配、并把 `degraded` 翻成 False
    # ——等于拿一次风暴冒充整份事件语料。所以 topStorm 走 overlay 文件。

    # ---- Agent 舰队 ------------------------------------------------------
    # OTR 只给了 name/mode/threshold，**没有任何应用绑定**，所以建得出节点、
    # 建不出 agent_watches 边。宁可留孤岛，也不要编一条"它监视谁"。
    for fleet, members in snap.get("agents", {}).items():
        for m in members:
            slug = slugify(m["name"])
            nodes["agent"].append(
                _node(
                    f"agent:{slug}",
                    "agent",
                    slug,
                    display_name=m["name"],
                    description=f"{fleet} 舰队的智能体（运行模式 {m['mode']}）。",
                    attributes={"fleet": fleet, "mode": m["mode"], "threshold": m["threshold"]},
                    keywords=derive_keywords(m["name"]),
                    notes="源：otr.json snapshot.agents；无 agent_watches 边——OTR 未给出监视对象",
                )
            )

    edges.extend(runtime_edges)

    # ---- 组装 ------------------------------------------------------------
    derived = dict(old["metadata"].get("derived_rules", {}))
    derived["otr_tenant_import"] = {
        "note": (
            "业务层（enterprise/journey/portfolio）与 50 个业务应用来自 otr.json，以它为准。"
            "app 的 source=otr-inventory：OTR 是**业务盘点**，不含 owner/namespace/runtime/仓库，"
            "这些字段一律为 null——未知不编造。"
        ),
        "generated": True,
        "source_file": "service-intelligence-platform-ui/tools/tenant-data/otr.json",
        "regenerate": "python3 scripts/import_otr_tenant.py",
    }
    derived["bridge_to_runtime_apps"] = {
        "note": (
            "⚠️ **人工判断，不是 OTR 数据**：把 10 个保留下来的 k8s 服务按语义挂进 OTR 业务域，"
            "使业务层与运行时层连通。映射表见 scripts/import_otr_tenant.py 的 BRIDGE。"
            "这是全图里唯一一处非来源数据，改它不需要重新导入。"
        ),
        "generated": True,
        "mapping": BRIDGE,
    }
    derived["business_tier_not_topology_tier"] = {
        "note": (
            "OTR 的 tier(1|2|3) 是**业务分级**，落在 attributes.business_tier；"
            "AppAttributes.tier 是**拓扑层**(edge/core/support)，两者不同轴，不要互相赋值。"
        ),
        "generated": True,
    }
    derived["runtime_layer_preserved"] = {
        "note": (
            "10 个 k8s 服务及其 13 条 calls、10 条 app_codebase 边原样保留。"
            "otr.json **完全没有**调用拓扑与仓库信息（app 之间零条边），"
            "若按字面删掉这一层，get_service_topology 与 locate_repo 会同时失去数据源。"
        ),
        "generated": True,
    }

    # ``metadata.source`` 要**幂等**：它是对旧值做追加，重复导入就会把同一段后缀
    # 叠成两遍、三遍。先把上一次追加过的部分切掉，再追加——这样"再跑一次"能回到同一份
    # 文件（否则"重新导入回基线"这件事根本做不到，页面上的编辑也就没法回滚）。
    suffix = (
        f"OTR 租户整合（enterprise/journey/portfolio + 50 业务应用），"
        f"源 {otr_path.name}，schema {SCHEMA_VERSION}"
    )
    base_source = old["metadata"]["source"]
    if _SOURCE_MARK in base_source:
        base_source = base_source.split(_SOURCE_MARK)[0].rstrip("; ")

    main_doc = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "source": f"{base_source}; {suffix}",
            "derived_rules": derived,
        },
        "ontology": old["ontology"],
        "nodes": nodes,
        "edges": edges,
    }

    # ---- 事件覆盖层（独立文件，默认不加载）--------------------------------
    # OTR 唯一的事件数据就是 topStorm 那一次风暴。按 §6 它该在覆盖层里——
    # 用 DATASOURCE_INCIDENTS_PATH 指过来才生效；不指，就是"没有事件数据"，
    # 这是**合法状态**而非配置错误。
    storm = snap["topStorm"]
    storm_app = f"app:{storm['appKey']}"
    if storm_app not in {a["id"] for a in snap["apps"]}:
        raise SystemExit(f"topStorm.appKey 指向不存在的 OTR 应用：{storm_app}")
    inc_slug = storm["incidentNumber"].lower()
    overlay_doc = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "source": f"OTR 事件覆盖层，源 {otr_path.name} snapshot.topStorm",
            "derived_rules": {
                "overlay_not_auto_loaded": {
                    "note": (
                        "本文件**不会被自动加载**：需要把 DATASOURCE_INCIDENTS_PATH 指向它。"
                        "不指 = 没有事件数据，走 degraded 标记——这是合法状态，不是配置错误"
                        "（docs/cmdb-entities.md §6）。"
                    ),
                    "generated": True,
                }
            },
        },
        # ontology 刻意省略——由主图提供，避免两份声明漂移（§6）。
        "nodes": {
            "incident": [
                _node(
                    f"incident:{inc_slug}",
                    "incident",
                    inc_slug,
                    display_name=storm["incidentNumber"],
                    description=(
                        f"{storm['appName']} 上的告警风暴，扇出 {storm['childCount']} 条子告警；"
                        f"{storm['openedAt']} — {storm['resolvedAt']}。"
                    ),
                    attributes={
                        "priority": storm["priority"],
                        "severity": storm["severity"],
                        "child_count": storm["childCount"],
                        "opened_at": storm["openedAt"],
                        "resolved_at": storm["resolvedAt"],
                    },
                    keywords=derive_keywords(storm["incidentNumber"], storm["appName"]),
                    notes="源：otr.json snapshot.topStorm / tenant.highlightEntities.stormIncident",
                )
            ],
            "change": [],
        },
        "edges": [
            _edge(
                f"e:incident_cluster_app:{inc_slug}->{storm['appKey']}",
                "incident_cluster_app",
                f"incident:{inc_slug}",
                storm_app,
            )
        ],
    }
    return main_doc, overlay_doc


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--otr", type=Path, default=DEFAULT_OTR,
        help=f"otr.json 路径（默认 {DEFAULT_OTR}）",
    )
    ap.add_argument("--cmdb", type=Path, default=DEFAULT_CMDB, help="cmdb-entities.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    args = ap.parse_args()

    if not args.otr.is_file():
        print(f"✗ 找不到 OTR 快照：{args.otr}", file=sys.stderr)
        return 2
    if not args.cmdb.is_file():
        print(f"✗ 找不到 CMDB 实体文件：{args.cmdb}", file=sys.stderr)
        return 2

    main_doc, overlay_doc = build(args.otr, args.cmdb)
    overlay_path = args.cmdb.with_name("cmdb-incidents-otr.json")

    print("主文件 nodes: " + ", ".join(f"{k}={len(v)}" for k, v in main_doc["nodes"].items() if v))
    print(f"主文件 edges: {len(main_doc['edges'])}")
    print(
        f"覆盖层 nodes: incident={len(overlay_doc['nodes']['incident'])}, "
        f"edges={len(overlay_doc['edges'])}"
    )

    if args.dry_run:
        print("（--dry-run：未落盘）")
        return 0

    for path, doc in ((args.cmdb, main_doc), (overlay_path, overlay_doc)):
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✓ 已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
