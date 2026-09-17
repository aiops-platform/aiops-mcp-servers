"""领域型只读工具：数据源查询（ES / Prometheus / K8s）+ CMDB 图谱（拓扑 / 图查询 / 候选推断）。

**全部工具 readOnlyHint=True**——agent 侧据此自动放行。因此**任何工具都不得写文件**，
CMDB 实体文件只读；事件数据走独立的只读覆盖层。

设计要点（design-v5.5 §4/§5）：

- **查询必须指定时间区间与目标**：`query_logs` / `get_trace` / `query_metrics`
  的 `start_time` / `end_time` 是必填参数，区间经 fail-closed 校验（格式、先后、
  跨度上限）后才下发；响应回显解析后的窗口，让调用方确知实际查了什么。
- **`check_infra` / `describe_pod` 无时间参数**——查的是集群当前状态，
  「查询目标」即 namespace / pod。
- 签名**字面书写**（不用 exec 生成），`Annotated[..., Field(...)]` 让 FastMCP
  生成 JSON Schema 并做真实校验。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field

from aiops_datasource_mcp_server.backends import cmdb, entity_graph, es, graph_query, k8s
from aiops_datasource_mcp_server.backends import prometheus as prom
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

# ----------------------------------------------------------------------
# 共用参数类型（中英双语描述，与 git-mcp-server 风格一致）
# ----------------------------------------------------------------------
StartTime = Annotated[
    str,
    Field(description=(
        "开始时间（ISO8601，含边界），如 2026-09-10T06:00:00 或 "
        "2026-09-10T06:00:00+00:00。必填——本 server 不提供无窗口的全量查询。"
    )),
]
EndTime = Annotated[
    str,
    Field(description=(
        "结束时间（ISO8601，含边界），须晚于 start_time。"
        f"区间跨度上限 {get_settings().datasource_max_range_hours:g} 小时。"
    )),
]
ServiceType = Annotated[
    str,
    Field(description="服务名（如 order-service / warranty-service），作为查询目标"),
]
NamespaceType = Annotated[
    str,
    Field(description="K8s namespace，如 order"),
]

# 注意：本模块用了 `from __future__ import annotations`，函数签名的注解会被存成**字符串**
# 交给 FastMCP 的 get_type_hints 惰性求值。因此注解里引用的名字必须能在**模块全局**解析——
# 不能用函数内闭包变量（那样 InvalidSignature）。描述文本一律提升到模块级常量。
_METRIC_DESC = (
    "指标名（**领域语义，不是 PromQL 表达式**）。可用："
    + ", ".join(prom.available_metrics())
    + "。传其它值会直接报错并列出可用项——本 server 不做静默兜底。"
)

# 同理：默认跳数与其描述也必须是**模块级**（闭包变量在 get_type_hints 里解析不到，
# 会报 InvalidSignature）
_TOPOLOGY_DEFAULT_HOPS = get_settings().datasource_topology_default_hops
_TOPOLOGY_HOPS_DESC = (
    f"查询跳数（上游/下游各展开几层），默认 {_TOPOLOGY_DEFAULT_HOPS}"
)

# 图查询工具的默认值同样是**模块级字面量**。这里刻意不读 settings：没有第二个跳数
# 配置项的真实需求（YAGNI）；将来若确实要可配，必须照 _TOPOLOGY_DEFAULT_HOPS 的写法
# 提升到模块级常量，否则 get_type_hints 解析闭包变量会报 InvalidSignature。
_GRAPH_FOCUS_HOPS = 1
_INFER_MAX_HOPS = 1
_INFER_LIMIT = 10

# 注：这些描述**故意保持静态**，不在 import 时读实体文件去拼合法取值清单——
# 那样会让模块在文件损坏时直接 import 失败（违背 lazy fail-closed 的设计），
# 且会把词表冻结在 import 时刻。合法取值由响应里的 `facets` 现算给出。
_NODE_TYPES_DESC = (
    "节点类型过滤（NODE TYPE 维度）。合法取值见响应 facets.node_types；"
    "传未知值会报错并列出合法项。"
)
_PORTFOLIOS_DESC = (
    "业务域过滤（PORTFOLIO 维度）。取值为 Portfolio 节点名（见 facets.portfolios）；"
    "命中该业务域下的应用。"
)
_KEY_ATTRIBUTES_DESC = (
    "静态横切标签过滤（KEY ATTRIBUTES 维度）。取值为 ontology 声明的静态标签"
    "（见 facets.key_attributes）。**派生指标（Top 10 by incidents 等带时间窗口的）"
    "不是静态标签，不能作为过滤值**。"
)
_EDGE_TYPES_DESC = (
    "边类型过滤（EDGE TYPE 维度）。合法取值见响应 facets.edge_types。"
)
_GRAPH_NODE_ID_DESC = (
    "聚焦节点 id（如 app:order-service），以该节点为中心展开邻域；"
    "不传则返回全图（受其它维度过滤）。"
)
_GRAPH_HOPS_DESC = f"聚焦展开跳数，默认 {_GRAPH_FOCUS_HOPS}"
_INCLUDE_FACETS_DESC = "是否返回 facets（四个维度的可用取值与计数）。默认 true。"

_PROBLEM_DESC = (
    "问题 / 故障描述（自然语言，中英不限）。仅用于与静态实体字段做**关键词子串**匹配"
    "——本 server 没有模型，不做语义理解。"
)
_SYMPTOM_SERVICES_DESC = (
    "**强烈建议传入**：已从日志 / 链路确认的症状服务名（如 warranty-service）。"
    "这比文本匹配强得多；命中者置信 high，其依赖邻居置信 medium。"
)
_INFER_NAMESPACES_DESC = "已确认涉及的 namespace（可选），命中者作为弱证据。"
_INFER_MAX_HOPS_DESC = f"沿依赖边扩展的跳数，默认 {_INFER_MAX_HOPS}"
_INFER_LIMIT_DESC = f"最多返回的候选数，默认 {_INFER_LIMIT}"



def _parse_window(start_time: str, end_time: str) -> tuple[datetime, datetime]:
    """解析并校验时间区间（fail-closed）。

    任一不合法即 ``AppError(INVALID_REQUEST)``——**不做宽容解析、不静默取默认值**：
    查询窗口错了，返回的数据就没有意义，早失败远好过给出一份看似正常的答案。
    """
    def _parse(raw: str, field: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"{field} 不是合法 ISO8601 时间：{raw!r}。"
                "示例：2026-09-10T06:00:00 或 2026-09-10T06:00:00Z",
            ) from exc
        # naive 时间视为 UTC，与 ES / Prometheus 的时区语义对齐
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    start = _parse(start_time, "start_time")
    end = _parse(end_time, "end_time")
    if start >= end:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"start_time 必须早于 end_time（当前 {start.isoformat()} ≥ {end.isoformat()}）",
        )

    max_hours = get_settings().datasource_max_range_hours
    span_hours = (end - start).total_seconds() / 3600
    if span_hours > max_hours:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"时间区间 {span_hours:.1f} 小时超过上限 {max_hours:g} 小时——"
            "请收窄窗口（诊断应聚焦故障发生的那一段时间）",
        )
    return start, end


# ======================================================================
# 工具工厂
# ======================================================================
def _build_query_logs():
    async def query_logs(
        start_time: StartTime,
        end_time: EndTime,
        service: Annotated[
            str | None,
            Field(description="服务名过滤（如 warranty-service）。**与 level 至少给一个**"),
        ] = None,
        level: Annotated[
            str | None,
            Field(description="日志级别过滤，如 ERROR / WARN / INFO。**与 service 至少给一个**"),
        ] = None,
        offset: Annotated[
            int, Field(ge=0, description="分页起点（默认 0）。offset+limit 不得超过 10000")
        ] = 0,
        limit: Annotated[
            int, Field(ge=1, le=200, description="本页最多返回条数（1-200，默认 50）")
        ] = 50,
    ) -> dict:
        """按**时间窗口 + 至少一个选择性条件**检索应用日志（Elasticsearch）。

        ⚠️ **时间区间是「范围」不是「选择」**——它不缩小结果集，只圈定"哪一段"。
        真实系统里一小时也能有 GB 级日志，所以 `service` / `level` **至少要给一个**，
        否则直接报错（不会"顺手"把整个窗口拉出来）。

        ## 怎么读返回体

        - `total` 是**真实命中数**；`total_relation="gte"` 时它是**下界**（超过 10000 条
          的统计上限）。**`returned` 才是本次返回的条数**——两者不要混。
        - `by_service` / `by_level` 是 **terms 聚合**算出的**全量**分布，不受分页影响。
          想知道"哪些服务在报错、各多少条"，看这两个就够了，**不必翻页**。
        - `has_more` / `offset`：要继续取用 `offset` 翻页，但**别靠深翻页取全量**
          （10000 条上限），要缩小范围请加筛选或缩时间窗。
        """
        start, end = _parse_window(start_time, end_time)
        return await es.query_logs(
            start, end, service=service, level=level, offset=offset, limit=limit
        )

    return query_logs


def _build_get_trace():
    async def get_trace(
        trace_id: Annotated[
            str, Field(description="链路 ID（traceId），按它重建整条调用链")
        ],
        start_time: StartTime,
        end_time: EndTime,
        limit: Annotated[
            int, Field(ge=1, le=200, description="链路内最多取多少条日志（默认 100）")
        ] = 100,
    ) -> dict:
        """按 trace_id + **时间窗口**重建调用链，判定故障 span。

        `failing_service` 是对故障服务的推断：优先采信「业务根因」类错误，
        把 feign / Read timed out 之类视为下游调用症状（即表面现象）排除在外。
        """
        start, end = _parse_window(start_time, end_time)
        return await es.get_trace(trace_id, start, end, limit=limit)

    return get_trace


def _build_query_metrics():
    async def query_metrics(
        service: ServiceType,
        metric: Annotated[str, Field(description=_METRIC_DESC)],
        start_time: StartTime,
        end_time: EndTime,
        step_seconds: Annotated[
            int,
            Field(ge=5, le=3600, description="采样步长（秒），默认 30"),
        ] = 30,
    ) -> dict:
        """查询服务在**时间窗口**内的指标（Prometheus query_range）。

        返回窗口内聚合（峰值/均值/最小/最后）与序列点。`value` 取**峰值**——
        诊断关心的是"有没有打满"，不是平均值。

        指标为 `cpu_percent` / `memory_percent` / `disk_percent` /
        `error_rate` / `p95_latency_ms`；无数据时 `value` 为 null（例如容器未设
        内存 limit），**不要把 null 当成 0**。
        """
        start, end = _parse_window(start_time, end_time)
        return await prom.query_range(service, metric, start, end, step_seconds)

    return query_metrics


def _build_check_infra():
    async def check_infra(
        namespace: Annotated[
            str | None, Field(description="K8s namespace；不传则用服务端默认配置")
        ] = None,
        pod: Annotated[
            str | None,
            Field(description="Pod 名或服务名（按 app 标签匹配）；不传则列出该 namespace 全部 Pod"),
        ] = None,
    ) -> dict:
        """查询 Pod 状态与重启次数（K8s **当前状态**，无时间参数）。

        不传 `pod` 时返回该 namespace 的 Pod 概览列表。
        """
        return await k8s.check_infra(namespace=namespace, pod=pod)

    return check_infra


def _build_get_service_topology():
    async def get_service_topology(
        service: ServiceType,
        hops: Annotated[
            int, Field(ge=0, le=6, description=_TOPOLOGY_HOPS_DESC)
        ] = _TOPOLOGY_DEFAULT_HOPS,
    ) -> dict:
        """查服务的依赖拓扑：N 跳内的关联服务（**静态目录，非运行时观测**）。

        诊断上最常用的两个用途：
        - `upstream`（谁调用我）→ **爆炸半径**：这个服务挂了还会影响谁
        - `downstream`（我调用谁）→ **可能的上游根因**：我的问题是不是下游拖的

        返回每个关联服务的 `distance`（跳数）、`direction`（upstream/downstream/both）、
        以及目录信息（owner / tier / namespace / tech / criticality）。
        """
        return await cmdb.get_service_topology(service, hops)

    return get_service_topology


def _build_locate_repo():
    async def locate_repo(service: ServiceType) -> dict:
        """由服务名定位代码仓库（service → repo URL + owner/tier/namespace）。

        用于"症状发生在哪个服务 → 该去看哪个仓库的代码"。返回 `found=false` 表示
        CMDB 未收录该服务（**不要据此编造仓库**），并附可用服务名清单。
        """
        return await cmdb.locate_repo(service)

    return locate_repo


def _build_describe_pod():
    async def describe_pod(
        pod: Annotated[str, Field(description="Pod 名（完整名，含 deployment hash 与随机后缀）")],
        namespace: Annotated[
            str | None, Field(description="K8s namespace；不传则用服务端默认配置")
        ] = None,
    ) -> dict:
        """查看 Pod 详情（kubectl describe，含事件与资源水位；无时间参数）。"""
        return await k8s.describe_pod(namespace=namespace, pod=pod)

    return describe_pod


def _build_query_entity_graph():
    async def query_entity_graph(
        node_types: Annotated[
            list[str] | None, Field(description=_NODE_TYPES_DESC)
        ] = None,
        portfolios: Annotated[
            list[str] | None, Field(description=_PORTFOLIOS_DESC)
        ] = None,
        key_attributes: Annotated[
            list[str] | None, Field(description=_KEY_ATTRIBUTES_DESC)
        ] = None,
        edge_types: Annotated[
            list[str] | None, Field(description=_EDGE_TYPES_DESC)
        ] = None,
        node_id: Annotated[
            str | None, Field(description=_GRAPH_NODE_ID_DESC)
        ] = None,
        hops: Annotated[
            int, Field(ge=0, le=6, description=_GRAPH_HOPS_DESC)
        ] = _GRAPH_FOCUS_HOPS,
        include_facets: Annotated[
            bool, Field(description=_INCLUDE_FACETS_DESC)
        ] = True,
    ) -> dict:
        """按四个维度查询 CMDB 实体图谱（NODE TYPE / PORTFOLIO / KEY ATTRIBUTES / EDGE TYPE）。

        与 `get_service_topology` 的分工——**两者在 2 跳以上会给出不同结果，这是故意的**：

        - `get_service_topology` 只走 `calls` 一种边，方向**相对起点**定义，把"上游的
          其他下游"（兄弟节点）排除在外。判断**爆炸半径 / 根因**请用它。
        - 本工具跨**全部 11 类边**、按四个维度筛选，聚焦时按**无向邻域**展开（因为
          `portfolio_link` 等边根本没有方向）。用途是**探索结构**，不是诊断判定。

        返回 `facets`（四个维度的可用取值与**现算**的计数）与 `unavailable_dimensions`
        （整维为空时如实报出）。计数从不存储——空类型就是 `count: 0`，不会伪装成有数据。
        """
        return graph_query.query_graph(
            entity_graph.get_effective_graph(),
            node_types=node_types,
            portfolios=portfolios,
            key_attributes=key_attributes,
            edge_types=edge_types,
            node_id=node_id,
            hops=hops,
            include_facets=include_facets,
        )

    return query_entity_graph


def _build_infer_candidate_services():
    async def infer_candidate_services(
        problem: Annotated[str, Field(min_length=1, max_length=2000, description=_PROBLEM_DESC)],
        services: Annotated[
            list[str] | None, Field(description=_SYMPTOM_SERVICES_DESC)
        ] = None,
        namespaces: Annotated[
            list[str] | None, Field(description=_INFER_NAMESPACES_DESC)
        ] = None,
        max_hops: Annotated[
            int, Field(ge=0, le=3, description=_INFER_MAX_HOPS_DESC)
        ] = _INFER_MAX_HOPS,
        limit: Annotated[
            int, Field(ge=1, le=50, description=_INFER_LIMIT_DESC)
        ] = _INFER_LIMIT,
    ) -> dict:
        """由问题描述推断**候选应用**，供诊断聚焦。

        三档证据：症状服务（强）→ 依赖拓扑扩展 → 问题文本子串匹配（弱）。
        每个候选都带**非空的 reasons**（必须能追溯到具体事实）与置信档位
        （`high` / `medium` / `low`，**不给浮点分**——那会暗示一个不存在的校准模型）。

        **业务域消歧**：同名应用可能挂在多个业务域下（实测 `VLMS` 同时属于
        Work Order / Handover / Workshop）。每个候选带 `business_paths`
        （enterprise / journey / portfolio / domain 路径）；返回体另有 `matched_domains`
        （**输入文本命中到的业务域**，空列表 = 输入里没有域线索）与每个候选的 `in_domain`
        （`null` 表示无域线索，**不等于**"不在域内"）。

        头部候选**同分却分属不同业务域**时 `ambiguous=true`、summary 里点明——
        **此时不要替调用方挑一个**，应澄清属于哪个业务域（design-v5.7 §3.6）。

        本 CMDB 目前没有 Incident / Change 记录，故 `degraded=true`、未走「事件 → 应用」
        路径。**未命中任何服务时请勿编造服务名**，请改用日志 / 链路确认症状服务后重试。
        """
        return graph_query.infer_candidates(
            entity_graph.get_effective_graph(),
            problem=problem,
            services=services,
            namespaces=namespaces,
            max_hops=max_hops,
            limit=limit,
        )

    return infer_candidate_services


FACTORIES = {
    "query_logs": _build_query_logs,
    "get_service_topology": _build_get_service_topology,
    "locate_repo": _build_locate_repo,
    "get_trace": _build_get_trace,
    "query_metrics": _build_query_metrics,
    "check_infra": _build_check_infra,
    "describe_pod": _build_describe_pod,
    "query_entity_graph": _build_query_entity_graph,
    "infer_candidate_services": _build_infer_candidate_services,
}
