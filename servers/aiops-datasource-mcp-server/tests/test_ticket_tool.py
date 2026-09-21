"""`returnApmTicketStatus`：本 server 唯一的写工具。

测三件事：

1. **谓词 + URI 走配置**，不由调用方传（地址是部署属性，不是 run 的属性）；
2. **fail-closed**：没配 URL / status 非法 / description 空 → 报错，**绝不静默成功**；
3. **上游失败要说真话**：4xx/5xx 归一成 `success: false` + 真实原因，让调用方如实
   上报"未投递"（agentflow 侧据此把节点判红，见 VERDICT_FIELDS）。
"""
from __future__ import annotations

import httpx
import pytest

from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode
from aiops_datasource_mcp_server.tools import register
from aiops_datasource_mcp_server.tools.datasource import FACTORIES as READ_ONLY_FACTORIES
from aiops_datasource_mcp_server.tools.ticket import (
    FACTORIES as WRITE_FACTORIES,
    _build_return_apm_ticket_status,
)

URL = "https://apm.internal/api/v1/incidents/callback"


@pytest.fixture
def configured(monkeypatch):
    """配好「谓词 + URI」。

    `get_settings` 是 `lru_cache` 的，setenv 之后必须显式 clear —— conftest 的
    `clear_settings_cache` 夹具只在**测试开始前**清一次，覆盖不到测试体内改的 env。
    """
    _configure(monkeypatch, method="POST")
    return URL


def _configure(monkeypatch, *, url: str = URL, method: str = "POST") -> None:
    monkeypatch.setenv("DATASOURCE_APM_TICKET_URL", url)
    monkeypatch.setenv("DATASOURCE_APM_TICKET_METHOD", method)
    get_settings.cache_clear()


def _install_transport(monkeypatch, handler):
    """把工具里的 `fetch_json` 接上 MockTransport。

    刻意**不替换 fetch_json 本身**——真实的请求构造（method / json_body）、
    超时、字节上限与失败归一化都还要跑。只换掉网络那一层。
    """
    from aiops_datasource_mcp_server.backends.http import fetch_json as real

    import aiops_datasource_mcp_server.tools.ticket as ticket

    async def patched(url, **kw):
        return await real(url, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(ticket, "fetch_json", patched)


# ── 谓词 + URI + payload 真的发出去了 ────────────────────────────────
async def test_posts_configured_verb_and_uri_with_payload(configured, monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        import json

        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True, "incident": "INC95528"})

    _install_transport(monkeypatch, handler)
    out = await _build_return_apm_ticket_status()(
        ticket_id="INC95528", status="failed", description="根因：数据盘写满",
    )

    assert seen["method"] == "POST"
    assert seen["url"] == URL, "URI 必须来自配置，不是调用方传的"
    assert seen["body"] == {
        "ticket_id": "INC95528", "status": "failed", "description": "根因：数据盘写满",
    }
    assert out["delivered"] is True
    assert out["success"] is True


async def test_method_comes_from_config(monkeypatch) -> None:
    """谓词同样是配置项（原系统的回调端点不一定是 POST）。"""
    _configure(monkeypatch, method="put")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    await _build_return_apm_ticket_status()(
        ticket_id="INC1", status="resolved", description="已修复",
    )
    assert seen["method"] == "PUT", "配置值应被归一化且生效"


# ── fail-closed ─────────────────────────────────────────────────────
async def test_missing_url_fails_closed(monkeypatch) -> None:
    """**没配回调地址 ⇒ 报错**，不是静默返回成功。

    这是本工具存在的前提：投不出去必须是响的。静默成功会让 agent 报
    `delivered: true`、run 变绿，而原系统那边什么都没收到。
    """
    _configure(monkeypatch, url="")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("未配置地址时**不应发起任何请求**")

    _install_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await _build_return_apm_ticket_status()(
            ticket_id="INC95528", status="failed", description="…",
        )
    assert ei.value.code == ErrorCode.CONFIG_ERROR
    assert "DATASOURCE_APM_TICKET_URL" in ei.value.message


@pytest.mark.parametrize("bad", ["done", "RESOLVED", "", "fixed"])
async def test_invalid_status_rejected_before_network(configured, monkeypatch, bad: str) -> None:
    """status 非法 → 报错并列出可用项，**且不发请求**。

    原系统的状态机只认那几个词，多一个就落不了库。本 server 按惯例不做静默兜底
    （同 query_metrics 对未知 metric 的处理）。
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("status 非法时不应发起请求")

    _install_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await _build_return_apm_ticket_status()(
            ticket_id="INC95528", status=bad, description="…",
        )
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "resolved" in ei.value.message  # 列出可用项


async def test_empty_description_rejected(configured, monkeypatch) -> None:
    """空 description ⇒ 拒绝——别把一个没有结论的空壳写进别人的系统。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("description 为空时不应发起请求")

    _install_transport(monkeypatch, handler)
    with pytest.raises(AppError) as ei:
        await _build_return_apm_ticket_status()(
            ticket_id="INC95528", status="failed", description="   ",
        )
    assert ei.value.code == ErrorCode.INVALID_REQUEST


# ── 上游失败要说真话 ────────────────────────────────────────────────
async def test_upstream_5xx_reports_not_delivered(configured, monkeypatch) -> None:
    """上游 500 ⇒ `delivered: false` + 真实原因（**不抛、不吞**）。

    不抛是因为调用方（agent）要拿这个结果去如实填自己的 `delivered` ——
    抛出去只会让节点挂在异常上，丢掉的正是"为什么没投出去"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    _install_transport(monkeypatch, handler)
    out = await _build_return_apm_ticket_status()(
        ticket_id="INC95528", status="failed", description="…",
    )
    assert out["delivered"] is False
    assert out["success"] is False
    assert out["upstream_status"] == 500
    assert "500" in str(out["response"]), "真实原因必须带出来"


async def test_upstream_unreachable_reports_not_delivered(configured, monkeypatch) -> None:
    """连不上 ⇒ 同样 `delivered: false`（这是最容易被写成"成功"的一种）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, handler)
    out = await _build_return_apm_ticket_status()(
        ticket_id="INC95528", status="resolved", description="…",
    )
    assert out["delivered"] is False
    assert out["response"], "必须说明为什么没投出去"


# ── 注册面的 readOnlyHint ───────────────────────────────────────────
class _RecordingServer:
    def __init__(self) -> None:
        self.annotations: dict[str, bool] = {}

    def tool(self, *, name, annotations):
        self.annotations[name] = annotations.readOnlyHint

        def _deco(fn):
            return fn

        return _deco


def test_write_tool_registered_as_not_read_only() -> None:
    """**写工具不得标成只读。**

    `readOnlyHint` 不是装饰性元数据：agent 侧（AgentScope）据此自动放行工具。
    把 returnApmTicketStatus 标成只读 = 让模型无需任何授权就能 POST 到外部系统。
    """
    server = _RecordingServer()
    names = register(server)

    assert set(WRITE_FACTORIES) == {"returnApmTicketStatus"}
    assert server.annotations["returnApmTicketStatus"] is False, "写工具必须是 readOnlyHint=False"
    # 只读那批不受影响
    assert all(server.annotations[n] is True for n in READ_ONLY_FACTORIES)
    assert set(names) == set(READ_ONLY_FACTORIES) | set(WRITE_FACTORIES)
