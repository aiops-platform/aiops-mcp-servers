"""工单回传：`returnApmTicketStatus` —— **本 server 唯一的对外写面**。

## 为什么单独一个模块，不并入 datasource.py

`datasource.py` 的模块 docstring 里写着一条不变式：

    全部工具 readOnlyHint=True —— 因此**任何工具都不得写文件**。

agent 侧（AgentScope）正是据 `readOnlyHint` 自动放行工具的。本工具**违反**那条
不变式：它 POST 到外部系统、会真的改掉人家的工单状态。塞进 `FACTORIES` 会让那句
docstring 变成假话——后来人照着它写代码就会踩。所以这里单独成模块，注册时**显式**
标 `readOnlyHint=False`（见 `tools/__init__.py`）。

## 「谓词 + URI」由 server 配置，不由 agent 传

回调地址是**部署属性**，不是这次 run 的属性。早先的形态是把工单入参里的
`callback_url` 一路透传到 agent 的输出里，有两个问题：

1. **安全**：那个值是 LLM 组装的输出，等于让模型决定往哪个地址 POST；
2. **重复**：同一个原系统，每张工单都得重新传一遍，传错或漏传就静默投不出去。

现在 `DATASOURCE_APM_TICKET_URL` / `DATASOURCE_APM_TICKET_METHOD` 配一次，
agent 只负责三个业务字段：`ticket_id` / `status` / `description`。

## fail-closed（本模块最重要的一条）

- **没配 URL ⇒ 报错**，不是静默返回成功。
- **status 不在合法取值内 ⇒ 报错**并列出可用项（与 `query_metrics` 对未知 metric
  的处理一致：本 server 不做静默兜底）。
- **上游返回 4xx/5xx ⇒ `success: false` + 真实原因**（经 `fetch_json` 归一化），
  **不抛异常**——调用方（agent）要拿这个结果去如实填 `delivered`。

这条链的终点在 agentflow 侧：`delivered: false` 会让 ticket-done 节点判 **FAILED**
（`VERDICT_FIELDS`），run 变红。**投不出去必须是红的**——绿着一条没投出去的 run
比红着更危险，看板会把它算成已闭环。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from aiops_datasource_mcp_server.backends.http import fetch_json
from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

#: 合法工单状态。**由调用方（原系统）的状态机决定，不是本 server 发明的**——
#: 多一个词人家就落不了库。与 agentflow 的 ticket-done system_prompt 里那份清单
#: 必须一致（那边写死的是同一组；两处都改才算改完）。
_VALID_STATUS = ("resolved", "failed", "insufficient")

_STATUS_DESC = (
    "工单状态，取值**限定**为：" + ", ".join(_VALID_STATUS)
    + "。resolved=已修复并提交；failed=修复没走完；insufficient=证据不足、没定位到可改的东西。"
    "传其它值会直接报错——原系统的状态机只认这几种。"
)


def _build_return_apm_ticket_status():
    async def returnApmTicketStatus(
        ticket_id: Annotated[
            str,
            Field(description="工单号（如 INC95528），原样取自 run 入参的 ticket.number，不要编造"),
        ],
        status: Annotated[str, Field(description=_STATUS_DESC)],
        description: Annotated[
            str,
            Field(description=(
                "一段话的处置结论，面向运维值班同事：讲清根因是什么、做了什么处置、"
                "还有什么没做。**不要贴代码或 diff**。"
            )),
        ],
    ) -> dict:
        """把本次处置结果回传原系统，更新工单状态（**写操作**）。

        回调地址与 HTTP 谓词由 server 侧配置，本工具不接受地址参数。
        成功返回 `delivered: true`；上游拒绝或连不上返回 `success: false` +
        真实原因（**此时必须如实上报未投递**，不要当成功）。
        """
        settings = get_settings()
        url = (settings.datasource_apm_ticket_url or "").strip()
        method = (settings.datasource_apm_ticket_method or "POST").strip().upper()

        if not url:
            raise AppError(
                ErrorCode.CONFIG_ERROR,
                "未配置 DATASOURCE_APM_TICKET_URL：工单回传地址是部署属性，必须由 "
                "server 侧配置。未配置时本工具 fail-closed —— 宁可报错，也不假装投递成功。",
                {"tool": "returnApmTicketStatus"},
            )
        if status not in _VALID_STATUS:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"status 取值非法：{status!r}。合法取值：{', '.join(_VALID_STATUS)}。",
                {"tool": "returnApmTicketStatus", "valid_status": list(_VALID_STATUS)},
            )
        # 空 description 会让原系统收到一条没有结论的工单更新——让调用方在这里就失败，
        # 而不是把一个空壳写进别人的系统。
        if not (description or "").strip():
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "description 不能为空：原系统要靠它说明这次处置的结论。",
                {"tool": "returnApmTicketStatus"},
            )

        payload = {"ticket_id": ticket_id, "status": status, "description": description}
        result = await fetch_json(
            url, method=method, json_body=payload, tool="returnApmTicketStatus"
        )
        delivered = bool(result.get("success"))

        return {
            "success": delivered,
            #: 显式给一个正着念的字段——调用方据此填它自己的 `delivered`，
            #: 不必去理解 `success` 的语义（两者同值，这里只是把话说白）。
            "delivered": delivered,
            "ticket_id": ticket_id,
            "status": status,
            "request": {"method": method, "url": url, "payload": payload},
            "upstream_status": result.get("upstream_status"),
            "took_ms": result.get("took_ms"),
            # 成功给上游回执；失败给真实原因（**不吞**，调用方要据此说明为什么没投出去）
            "response": result.get("data") if delivered else result.get("error"),
        }

    return returnApmTicketStatus


FACTORIES = {
    "returnApmTicketStatus": _build_return_apm_ticket_status,
}
