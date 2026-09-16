"""CMDB 写路径：节点 / 边的增删改——**写前全量校验、原子落盘**。

## 为什么写操作必须整份过一遍 ``build_graph``

CMDB 是诊断链的数据源。一条指向不存在节点的边、一个 id 前缀与 type 不符的节点，
**不会在读取时炸**——它会让 ``get_service_topology`` 返回一份看着正常的错答案。
所以这里不设"轻量校验"这条捷径：整份文档走**同一个**校验器，不过就整个写不进去
（宁可这次编辑失败，也不要一份半对的数据悄悄生效）。

校验通过后**原子替换**（临时文件 + ``os.replace``）：进程在写一半时被杀，留下的是
旧文件而不是半截 JSON——半截文件会让 CMDB 工具 fail-closed，整个诊断链跟着挂。

## 为什么删节点默认不级联

删一个被边引用的节点会留下悬空端点，校验器会拒。这里**不**顺手把那些边也删掉：
被引用意味着图里还有别的结构依赖它，静默级联会删掉用户没打算删的东西。
要级联就显式 ``cascade=True``，并把连同删掉的边**如实返回**。

## 为什么读的是"原始文档"而不是 EntityGraph

``EntityGraph`` 是**校验并派生过**的只读视图（属性被 pydantic 规范化、索引已建）。
写回要的是磁盘上那份原文的结构，所以这里始终 read → 改 dict → validate → write，
不绕 EntityGraph。代价是每次写都重读一次文件——对人工编辑的频率完全够用。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

from aiops_datasource_mcp_server.backends.entity_graph import (
    APP_REQUIRED_BY_SOURCE,
    NODE_ATTR_MODELS,
    GraphLoadError,
    build_graph,
    clear_graph_cache,
    clear_overlay_cache,
    resolve_cmdb_path,
)

#: 读-改-写之间必须互斥，否则两个并发编辑会有一个被静默吞掉（后写的覆盖先写的）。
#: 单进程锁够用：这个端点是给人用的编辑器，不是高并发 API。
_WRITE_LOCK = threading.Lock()


class AdminError(Exception):
    """写操作被拒绝。``status`` 供 HTTP 层直接用。"""

    def __init__(self, message: str, *, status: int = 400, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}


# ----------------------------------------------------------------------
# 读 / 校验 / 写
# ----------------------------------------------------------------------
def _path() -> Path:
    return Path(resolve_cmdb_path())


def read_doc() -> dict:
    """读当前实体文件原文。"""
    path = _path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdminError(f"CMDB 实体文件不可读：{path}（{exc.strerror}）", status=500) from exc
    except json.JSONDecodeError as exc:
        raise AdminError(
            f"CMDB 实体文件不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 — {exc.msg}",
            status=500,
        ) from exc


def _validate(doc: dict) -> None:
    """整份文档走真实校验器。不过就抛，**不落盘**。"""
    try:
        build_graph(doc, path=str(_path()))
    except GraphLoadError as exc:
        raise AdminError(
            f"改动未通过校验，已拒绝写入：{exc.message}",
            status=422,
            details=exc.details,
        ) from exc


def _write_atomic(doc: dict) -> None:
    """原子替换：写临时文件 → ``os.replace``。同目录保证 replace 是原子的。"""
    path = _path()
    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cmdb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            # 失败时别把临时文件留在 data/ 里——它会跟着 wheel 一起被打包
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as exc:
        raise AdminError(
            f"CMDB 实体文件写不进去：{path}（{exc.strerror}）。"
            f"若文件在只读挂载或包安装目录里，请把 DATASOURCE_CMDB_PATH "
            f"指向可写位置。",
            status=500,
        ) from exc

    # 加载缓存按 (path, mtime) 失效，mtime 变了自己就会重载；这里显式清一次是
    # 为了**不依赖文件系统时间戳的粒度**——同 ns 内的两次写入在粗粒度 fs 上可能
    # 拿到同一个 mtime，那时缓存会返回上一版。
    clear_graph_cache()
    clear_overlay_cache()


def _commit(doc: dict) -> dict:
    _validate(doc)
    _write_atomic(doc)
    return summary()


def summary() -> dict:
    """当前图的一句话概况——每次写后返回，让界面能确认改动真的生效了。"""
    graph = build_graph(read_doc(), path=str(_path()))
    return {
        "path": graph.path,
        "schema_version": graph.schema_version,
        "node_count": graph.node_count,
        "edge_count": len(graph.edges),
        "nodes_by_type": {
            k: len(v) for k, v in graph.nodes_by_type.items() if v
        },
    }


# ----------------------------------------------------------------------
# 节点
# ----------------------------------------------------------------------
_NODE_KEYS = {"id", "type", "name", "display_name", "description", "attributes",
              "tags", "keywords", "refs", "notes"}


def _find_bucket(doc: dict, node_type: str) -> list[dict]:
    nodes = doc.get("nodes")
    if not isinstance(nodes, dict) or node_type not in nodes:
        raise AdminError(
            f"未知节点类型 {node_type!r}；已声明的类型：{sorted(doc.get('nodes') or {})}",
            status=400,
        )
    return nodes[node_type]


def _find_node(doc: dict, node_id: str) -> tuple[str, dict]:
    for type_key, items in (doc.get("nodes") or {}).items():
        for node in items:
            if node["id"] == node_id:
                return type_key, node
    raise AdminError(f"节点不存在：{node_id}", status=404)


def _reject_unknown_keys(payload: dict) -> None:
    extra = sorted(set(payload) - _NODE_KEYS)
    if extra:
        raise AdminError(
            f"节点含未知字段 {extra}；允许的字段：{sorted(_NODE_KEYS)}", status=400
        )


def create_node(payload: dict) -> dict:
    """新增节点。``type`` 决定它进哪个桶；``id`` 必须形如 ``<type>:<slug>``。"""
    if not isinstance(payload, dict):
        raise AdminError("请求体必须是节点对象", status=400)
    _reject_unknown_keys(payload)
    for required in ("id", "type", "name"):
        if not payload.get(required):
            raise AdminError(f"节点缺少必填字段 {required!r}", status=400)

    with _WRITE_LOCK:
        doc = read_doc()
        node = {
            "id": payload["id"],
            "type": payload["type"],
            "name": payload["name"],
            "display_name": payload.get("display_name"),
            "description": payload.get("description"),
            "attributes": payload.get("attributes") or {},
            "tags": payload.get("tags") or [],
            "keywords": payload.get("keywords") or [],
            "refs": payload.get("refs") or {},
            "notes": payload.get("notes"),
        }
        _find_bucket(doc, node["type"]).append(node)
        return {**_commit(doc), "node": node}


def update_node(node_id: str, patch: dict) -> dict:
    """局部更新节点。**只改传进来的字段**；``attributes`` 是整体替换，不是合并。

    整体替换是刻意的：``attributes`` 的形状由节点类型决定（app 有 source 判别字段），
    按字段合并会让"删掉一个属性"变得无法表达。
    """
    if not isinstance(patch, dict):
        raise AdminError("请求体必须是字段对象", status=400)
    _reject_unknown_keys(patch)
    if "id" in patch or "type" in patch:
        raise AdminError(
            "id / type 不可改——它们是节点的身份与分桶依据；要换就删了重建",
            status=400,
        )

    with _WRITE_LOCK:
        doc = read_doc()
        _, node = _find_node(doc, node_id)
        for key, value in patch.items():
            if key in ("tags", "keywords") and value is None:
                value = []
            if key == "attributes" and value is None:
                value = {}
            node[key] = value
        return {**_commit(doc), "node": node}


def delete_node(node_id: str, *, cascade: bool = False) -> dict:
    """删除节点。默认**拒绝**：有边引用它时先让调用方自己决定。

    ``cascade=True`` 会连同引用它的边一起删，并在返回值里列出删了哪些——不静默。
    """
    with _WRITE_LOCK:
        doc = read_doc()
        type_key, node = _find_node(doc, node_id)

        referencing = [
            e for e in doc["edges"] if e["from"] == node_id or e["to"] == node_id
        ]
        if referencing and not cascade:
            raise AdminError(
                f"节点 {node_id} 仍被 {len(referencing)} 条边引用，未删除。"
                f"请先删这些边，或改用 cascade=true 一并删除。",
                status=409,
                details={"edges": [e["id"] for e in referencing]},
            )

        doc["nodes"][type_key] = [n for n in doc["nodes"][type_key] if n["id"] != node_id]
        removed_edges: list[str] = []
        if cascade and referencing:
            removed_ids = {e["id"] for e in referencing}
            doc["edges"] = [e for e in doc["edges"] if e["id"] not in removed_ids]
            removed_edges = sorted(removed_ids)

        return {**_commit(doc), "deleted_node": node_id, "deleted_edges": removed_edges}


# ----------------------------------------------------------------------
# 边
# ----------------------------------------------------------------------
def _default_edge_id(edge_type: str, src: str, dst: str) -> str:
    return f"e:{edge_type}:{src.split(':', 1)[-1]}->{dst.split(':', 1)[-1]}"


def create_edge(payload: dict) -> dict:
    """新增边。``id`` 可省略——省了就按 ``e:<type>:<from>-><to>`` 约定生成。

    端点是否存在、端点类型是否被该边类型允许、属性是否在词表内——全交给
    ``build_graph`` 判，这里不重复实现一遍（两份规则一定会漂移）。
    """
    if not isinstance(payload, dict):
        raise AdminError("请求体必须是边对象", status=400)
    extra = sorted(set(payload) - {"id", "type", "from", "to", "attributes"})
    if extra:
        raise AdminError(f"边含未知字段 {extra}", status=400)
    for required in ("type", "from", "to"):
        if not payload.get(required):
            raise AdminError(f"边缺少必填字段 {required!r}", status=400)

    with _WRITE_LOCK:
        doc = read_doc()
        src, dst, edge_type = payload["from"], payload["to"], payload["type"]
        edge = {
            "id": payload.get("id") or _default_edge_id(edge_type, src, dst),
            "type": edge_type,
            "from": src,
            "to": dst,
            "attributes": payload.get("attributes") or {},
        }
        doc["edges"].append(edge)
        return {**_commit(doc), "edge": edge}


def delete_edge(edge_id: str) -> dict:
    """删除边。边不承载结构（没有别的边依赖它），所以删边不需要级联考量。"""
    with _WRITE_LOCK:
        doc = read_doc()
        remaining = [e for e in doc["edges"] if e["id"] != edge_id]
        if len(remaining) == len(doc["edges"]):
            raise AdminError(f"边不存在：{edge_id}", status=404)
        doc["edges"] = remaining
        return {**_commit(doc), "deleted_edge": edge_id}


# ----------------------------------------------------------------------
# 表单 schema
# ----------------------------------------------------------------------
def describe_schema() -> dict:
    """给编辑界面用的表单 schema。

    ⚠️ **``EntityDocument.model_json_schema()`` 不包含每种节点类型的 attributes 模型**
    ——``Node.attributes`` 声明成 ``dict[str, Any]``，而 ``NODE_ATTR_MODELS`` 是旁表，
    pydantic 从模型图上看不到它。所以这里显式把每个已登记类型的 attributes schema
    单独给出来，界面据此渲染表单与做编辑器侧校验；**未登记的类型**（attributes 开放）
    退回自由 KV 编辑。
    """
    doc = read_doc()
    onto = doc.get("ontology") or {}
    return {
        "schema_version": doc.get("schema_version"),
        "node_types": onto.get("node_types", []),
        "edge_types": onto.get("edge_types", []),
        "key_attributes": onto.get("key_attributes", []),
        "derived_metrics": onto.get("derived_metrics", []),
        #: 节点类型的 attributes 用哪个 pydantic 模型校验；不在表里的 = 开放 KV。
        "attribute_models": {
            type_key: model.model_json_schema()
            for type_key, model in sorted(NODE_ATTR_MODELS.items())
        },
        #: 未登记 attributes 模型的类型——界面应退回自由键值编辑，而不是硬套表单。
        "open_attribute_types": sorted(
            t["key"] for t in onto.get("node_types", []) if t["key"] not in NODE_ATTR_MODELS
        ),
        #: **条件必填**：必填与否取决于**另一个字段的取值**（app 看 `source`）。
        #: JSON Schema 表达不了这种约束，所以 `attribute_models.app.required` 里只有
        #: `kind`——界面若只照那份 schema 渲染，就不会把 namespace / owner 标成必填，
        #: 用户填完一保存才被拒。`discriminator` 指明看哪个字段。
        "conditional_required": {
            type_key: {
                "discriminator": discriminator,
                "required_if": {v: list(fields) for v, fields in table.items()},
            }
            for type_key, discriminator, table in (
                ("app", "source", APP_REQUIRED_BY_SOURCE),
            )
        },
    }


__all__ = [
    "AdminError",
    "create_edge",
    "create_node",
    "delete_edge",
    "delete_node",
    "describe_schema",
    "read_doc",
    "summary",
    "update_node",
]
