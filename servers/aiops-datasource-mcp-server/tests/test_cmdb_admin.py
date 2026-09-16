"""CMDB 写路径：写前全量校验、原子落盘、删节点不静默级联、管理端点的开关与认证。

这里的每一条都在钉同一个立场：**一个能改诊断数据源的写端点，宁可拒绝，也不要有
一份"改了一半"的图生效。** 所以反例（写不进去的那些）比正例更重要。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import cmdb_admin
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.config import get_settings
from starlette.testclient import TestClient


@pytest.fixture
def cmdb(tmp_path, monkeypatch, clear_settings_cache) -> Path:
    """把**真实实体文件**复制到 tmp 并指过去——在真实数据上做变异，顺带保证基准合法。

    必须在副本上写：真实文件是随包分发的基线，也是"重新导入"的落点。

    ⚠️ **``setenv`` 之后必须再 ``cache_clear()`` 一次**。``get_settings`` 是
    ``lru_cache`` 的，而上面那行 ``eg.resolve_cmdb_path()`` 会**顺带把缓存暖热**
    （它内部就调 ``get_settings()``）——此后 ``setenv`` 改的环境变量再也进不去，
    写操作会直接落到真实数据文件上。这个坑第一次就是这么踩的：测试把
    ``order-service`` 连同它的边一起级联删掉了。
    """
    src = Path(eg.resolve_cmdb_path())
    dst = tmp_path / "cmdb-entities.json"
    shutil.copy2(src, dst)
    monkeypatch.setenv("DATASOURCE_CMDB_PATH", str(dst))
    get_settings.cache_clear()   # ← 必须在 setenv 之后，理由见上
    eg.clear_graph_cache()
    return dst


def _doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _node(node_id: str, node_type: str, name: str, **extra) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "display_name": extra.pop("display_name", name),
        "description": extra.pop("description", None),
        "attributes": extra.pop("attributes", {}),
        "tags": extra.pop("tags", []),
        "keywords": extra.pop("keywords", []),
        **extra,
    }


# ======================================================================
# 节点：正例
# ======================================================================
def test_create_node_persists_and_loads(cmdb) -> None:
    """写进去的节点要能被**真实加载器**读出来——不只是 JSON 里有这一段。"""
    r = cmdb_admin.create_node(_node("portfolio:test-domain", "portfolio", "test-domain"))
    assert r["nodes_by_type"]["portfolio"] == 12
    assert _doc(cmdb)["nodes"]["portfolio"][-1]["id"] == "portfolio:test-domain"
    # 落盘后重新加载，走的仍是同一个校验器
    assert eg.clear_graph_cache() is None
    assert "portfolio:test-domain" in eg.get_graph().nodes


def test_update_node_patches_only_given_fields(cmdb) -> None:
    """局部更新：没传的字段**不动**。"""
    before = _doc(cmdb)["nodes"]["portfolio"][0]
    original_name = before["name"]
    r = cmdb_admin.update_node(before["id"], {"display_name": "改名了"})
    after = r["node"]
    assert after["display_name"] == "改名了"
    assert after["name"] == original_name
    assert after["keywords"] == before["keywords"]


def test_update_node_replaces_attributes_wholesale(cmdb) -> None:
    """``attributes`` 整体替换而非合并——否则"删掉一个属性"无法表达。"""
    node_id = "app:xentry-workshop"     # OTR 侧应用，属性由 source=otr-inventory 决定
    target = next(n for n in _doc(cmdb)["nodes"]["app"] if n["id"] == node_id)
    assert "incidents_total" in target["attributes"]

    r = cmdb_admin.update_node(
        node_id,
        {"attributes": {"kind": "application", "source": "otr-inventory",
                        "business_tier": 1, "tech": None}},
    )
    # 没在 patch 里出现的键**消失**了——这正是"整体替换"与"合并"的区别
    assert "incidents_total" not in r["node"]["attributes"]
    assert "change_count" not in r["node"]["attributes"]


def test_partial_attributes_write_on_kubernetes_app_is_rejected(cmdb) -> None:
    """把 k8s 应用的 attributes 写残 → 422，且**文件逐字节未变**。

    这条钉的是"整体替换"的**安全边界**：替换是整体生效的，所以漏写 6 个运行时字段
    的后果不是"少了一个字段"，而是整个 app 目录契约被破坏。全靠 ``AppAttributes``
    的按来源校验器挡在落盘之前。
    """
    before = cmdb.read_bytes()
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.update_node(
            "app:order-service",
            {"attributes": {"kind": "application", "source": "kubernetes"}},
        )
    assert exc.value.status == 422
    assert "缺少必填字段" in exc.value.message
    assert "namespace" in exc.value.message      # 说清是哪些字段，不只说"少了点东西"
    assert cmdb.read_bytes() == before


# ======================================================================
# 节点：反例（写不进去）
# ======================================================================
def test_invalid_node_is_rejected_and_file_untouched(cmdb) -> None:
    """id 前缀与 type 不符 → 422，且**文件逐字节未变**。

    "拒绝"必须同时是"没写"，否则拒绝就只是把人拦在了半路、数据已经脏了。
    """
    before = cmdb.read_bytes()
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.create_node(_node("app:whatever", "portfolio", "whatever"))
    assert exc.value.status == 422
    assert cmdb.read_bytes() == before


def test_dangling_edge_rejected(cmdb) -> None:
    """边指向不存在的节点 → 拒。这是"看着正常的错答案"的典型来源。"""
    before = cmdb.read_bytes()
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.create_edge(
            {"type": "portfolio_link", "from": "portfolio:campaign", "to": "app:does-not-exist"}
        )
    assert exc.value.status == 422
    assert "不存在" in exc.value.message
    assert cmdb.read_bytes() == before


def test_edge_with_wrong_endpoint_types_rejected(cmdb) -> None:
    """端点存在但类型不被该边允许（``calls`` 只能是 app → app）→ 拒。"""
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.create_edge(
            {"type": "calls", "from": "portfolio:campaign", "to": "app:order-service"}
        )
    assert exc.value.status == 422


def test_update_node_cannot_change_identity(cmdb) -> None:
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.update_node("app:order-service", {"id": "app:renamed"})
    assert exc.value.status == 400


def test_unknown_field_rejected(cmdb) -> None:
    """多出来的字段直接拒——静默忽略会让人以为改动生效了。"""
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.create_node(_node("app:x", "app", "x", 颜色="蓝"))
    assert exc.value.status == 400


# ======================================================================
# 删除：不静默级联
# ======================================================================
def test_delete_referenced_node_refused_with_edge_list(cmdb) -> None:
    """被边引用的节点默认**拒删**，并如实列出是哪些边挡着。"""
    edges = [e["id"] for e in _doc(cmdb)["edges"] if e["from"] == "app:order-service"
             or e["to"] == "app:order-service"]
    assert edges, "前置条件：order-service 应当有边"

    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.delete_node("app:order-service")
    assert exc.value.status == 409
    assert set(exc.value.details["edges"]) == set(edges)


def test_delete_cascade_removes_and_reports_edges(cmdb) -> None:
    """显式级联：删掉边，并把删了哪些**如实返回**。"""
    r = cmdb_admin.delete_node("app:order-service", cascade=True)
    assert r["deleted_node"] == "app:order-service"
    assert r["deleted_edges"], "级联必须报告删掉了哪些边"
    doc = _doc(cmdb)
    assert all(n["id"] != "app:order-service" for n in doc["nodes"]["app"])
    assert all(e["id"] not in r["deleted_edges"] for e in doc["edges"])


def test_delete_unreferenced_node_needs_no_cascade(cmdb) -> None:
    cmdb_admin.create_node(_node("agent:lonely", "agent", "lonely"))
    r = cmdb_admin.delete_node("agent:lonely")
    assert r["deleted_edges"] == []


def test_delete_edge(cmdb) -> None:
    edge_id = _doc(cmdb)["edges"][0]["id"]
    cmdb_admin.delete_edge(edge_id)
    assert all(e["id"] != edge_id for e in _doc(cmdb)["edges"])
    with pytest.raises(cmdb_admin.AdminError) as exc:
        cmdb_admin.delete_edge(edge_id)
    assert exc.value.status == 404


# ======================================================================
# 写后立刻可查（热重载契约）
# ======================================================================
def test_write_is_visible_to_read_tools_immediately(cmdb) -> None:
    """改完**下一次查询即生效**，无需重启——这是给编辑界面留的钩子，得真的成立。

    缓存按 (path, mtime) 失效；这里断言的是端到端行为，不是缓存实现。
    """
    before = eg.get_graph().node("portfolio:campaign")
    assert before["display_name"] == "Campaign"

    cmdb_admin.update_node("portfolio:campaign", {"display_name": "营销活动"})

    assert eg.get_graph().node("portfolio:campaign")["display_name"] == "营销活动"


# ======================================================================
# 表单 schema
# ======================================================================
def test_describe_schema_covers_per_type_attribute_models(cmdb) -> None:
    """``EntityDocument.model_json_schema()`` **看不到** ``NODE_ATTR_MODELS``
    （``Node.attributes`` 是 ``dict[str, Any]``，旁表不在模型图上）。

    所以表单要用的 attributes schema 必须由这里单独给出——这条测试守着这件事，
    否则界面会以为"所有节点的 attributes 都是自由 KV"。
    """
    s = cmdb_admin.describe_schema()
    assert "app" in s["attribute_models"]
    app_props = s["attribute_models"]["app"]["properties"]
    assert "source" in app_props and "business_tier" in app_props
    assert s["open_attribute_types"] == sorted(
        {t["key"] for t in s["node_types"]} - set(s["attribute_models"])
    )
    assert "agent" in s["open_attribute_types"]


def test_schema_exposes_conditional_required(cmdb) -> None:
    """条件必填必须**显式暴露**——JSON Schema 表达不了它，界面否则会漏标。

    ``AppAttributes.model_json_schema()["required"]`` 里只有 ``kind``（其余字段都有
    默认值），但 ``source=kubernetes`` 时那 6 个运行时字段其实必填。界面只照 JSON Schema
    渲染，就不会显示"必填"，用户填完一保存才被拒——这正是"界面与校验器不一致"的典型。

    ⚠️ 这条**同时钉住两边一致**：暴露出来的字段表必须与 ``APP_REQUIRED_BY_SOURCE``
    逐条相同。校验器改了而 schema 没跟上（或反过来），这里就红。
    """
    from aiops_datasource_mcp_server.backends.entity_graph import APP_REQUIRED_BY_SOURCE

    s = cmdb_admin.describe_schema()
    cond = s["conditional_required"]["app"]
    assert cond["discriminator"] == "source"
    assert {k: sorted(v) for k, v in cond["required_if"].items()} == {
        k: sorted(v) for k, v in APP_REQUIRED_BY_SOURCE.items()
    }
    # 这些字段确实**不在** JSON Schema 的 required 里——所以才有必要单列
    assert set(s["attribute_models"]["app"]["required"]) == {"kind"}


# ======================================================================
# HTTP 面：开关、认证、错误映射
# ======================================================================
def test_admin_routes_absent_when_disabled(env, monkeypatch, clear_settings_cache) -> None:
    """未启用时**路径不存在**（404），而不是"存在但返回 403"。"""
    monkeypatch.setenv("DATASOURCE_ADMIN_ENABLED", "false")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    from aiops_datasource_mcp_server.server import build_app

    r = TestClient(build_app()).get("/admin/cmdb/schema")
    assert r.status_code == 404


def test_admin_defaults_on_in_development_off_in_production(
    env, monkeypatch, clear_settings_cache
) -> None:
    """未显式配置时的默认：development 开、production 关。

    production 默认关是关键——一个能改诊断数据源的写端点，不该因为"部署时忘了关"
    而暴露出去。
    """
    from aiops_datasource_mcp_server.config import Settings

    monkeypatch.delenv("DATASOURCE_ADMIN_ENABLED", raising=False)
    assert Settings(environment="development").admin_enabled is True
    assert Settings(environment="production", auth_token="t").admin_enabled is False
    explicit = Settings(
        environment="production", auth_token="t", datasource_admin_enabled=True
    )
    assert explicit.admin_enabled is True


def test_production_without_token_refuses_to_start(clear_settings_cache) -> None:
    from aiops_datasource_mcp_server.config import Settings

    with pytest.raises(ValueError, match="AUTH_TOKEN"):
        Settings(environment="production").validate_for_environment()


def test_admin_requires_bearer_when_token_configured(
    env, cmdb, monkeypatch, clear_settings_cache
) -> None:
    monkeypatch.setenv("DATASOURCE_ADMIN_ENABLED", "true")
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    from aiops_datasource_mcp_server.server import build_app

    c = TestClient(build_app())
    assert c.get("/admin/cmdb/schema").status_code == 401
    assert c.get("/admin/cmdb/schema", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.get("/admin/cmdb/schema", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_admin_http_crud_roundtrip(env, cmdb, monkeypatch, clear_settings_cache) -> None:
    """走真实 HTTP：建 → 改 → 删，并确认非法请求返回 422 而不是 500。"""
    monkeypatch.setenv("DATASOURCE_ADMIN_ENABLED", "true")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    from aiops_datasource_mcp_server.server import build_app

    c = TestClient(build_app())

    r = c.post("/admin/cmdb/nodes", json=_node("portfolio:http-made", "portfolio", "http-made"))
    assert r.status_code == 200, r.text
    assert r.json()["nodes_by_type"]["portfolio"] == 12

    r = c.put("/admin/cmdb/nodes/portfolio:http-made", json={"display_name": "改过"})
    assert r.status_code == 200, r.text
    assert r.json()["node"]["display_name"] == "改过"

    # 非法改动 → 422（不是 500），且带上真实原因
    r = c.post(
        "/admin/cmdb/edges",
        json={"type": "calls", "from": "app:nope", "to": "app:order-service"},
    )
    assert r.status_code == 422, r.text
    assert "ADMIN_REJECTED" in r.json()["error"]

    assert c.delete("/admin/cmdb/nodes/portfolio:http-made").status_code == 200
    assert c.get("/admin/cmdb/summary").json()["nodes_by_type"]["portfolio"] == 11


def test_admin_rejects_bad_json_with_400(env, cmdb, monkeypatch, clear_settings_cache) -> None:
    monkeypatch.setenv("DATASOURCE_ADMIN_ENABLED", "true")
    from aiops_datasource_mcp_server.config import get_settings

    get_settings.cache_clear()
    from aiops_datasource_mcp_server.server import build_app

    c = TestClient(build_app())
    r = c.post(
        "/admin/cmdb/nodes",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_write_preserves_file_mode(cmdb) -> None:
    """保存**不得**改变文件权限位。

    ``tempfile.mkstemp`` 建出来的是 0600，而 ``os.replace`` 会把临时文件的模式带到
    目标上——不显式继承的话，每次从编辑页保存都把实体文件改成 0600。本地单人用看不出来；
    文件在挂载卷上、由另一个身份的进程读时才发现读不了，且没人会想到是"某次保存改的"。
    """
    import stat
    from pathlib import Path as _Path

    path = _Path(cmdb_admin.resolve_cmdb_path())
    os.chmod(path, 0o644)
    before = stat.S_IMODE(path.stat().st_mode)

    cmdb_admin.update_node("portfolio:campaign", {"display_name": "改一下"})

    after = stat.S_IMODE(path.stat().st_mode)
    assert after == before, f"权限位被改了：{oct(before)} → {oct(after)}"
