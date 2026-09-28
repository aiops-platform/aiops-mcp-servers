"""两个工具的声明：`rollout_deployment`（写）与 `get_deployment_status`（读）。

**签名只收 `service`、不收 namespace/deployment/container** —— 那三个由本 server
自己的 `DEPLOY_OPS_TARGETS` 持有。理由见 `config.py` 里那段注释（一句话：它们是
集群侧的事实，也是租户边界所在；让平台/模型传就多一个"传错"的面，
而**滚成功但滚错地方比失败更难查**）。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from deploy_ops_mcp_server.backends import probe, rollout

_SERVICE_DESC = (
    "服务名（如 order-service）。本 server 按它查自己的部署目标表得到 "
    "namespace/deployment/container —— **不要传 namespace 或 deployment 名**，本工具不收。"
)


def _build_rollout_deployment():
    async def rollout_deployment(
        service: Annotated[str, Field(description=_SERVICE_DESC)],
        image: Annotated[
            str,
            Field(description=(
                "要部署的镜像引用，形如 `order-service:211eae4e2518`（由上游 CI 节点产出、"
                "人工审批过的那个 tag）。**原样传，不要自己拼**。"
            )),
        ],
    ) -> dict:
        """把某个服务的 Deployment 滚到指定镜像（**写操作**，会真的改线上）。

        四步：把镜像装进集群节点 → `set image` → 等滚动完成 → **回读 pod 的镜像**。

        - 成功：`{"success": true, "deployed": true, "observed_image": ...}`
        - 失败：`{"success": false, "deployed": false, "stage": ..., "error": ...}`
          —— **`stage` 指明卡在哪一步**（image_load / set_image / rollout_status / read_back），
          而且**本工具不会自动回滚**（`error` 里带了回滚命令）。

        ⚠️ 拿到 `success: false` **必须如实上报 `deployed: false`** —— 不要因为
        "审批已经过了"就当成成功。滚不上去而工单报「已解决」，是这条链上最坏的形态。
        """
        return await rollout.rollout_deployment(service=service, image=image)

    return rollout_deployment


def _build_get_deployment_status():
    async def get_deployment_status(
        service: Annotated[str, Field(description=_SERVICE_DESC)],
    ) -> dict:
        """读某个服务**当前在跑的**镜像与 pod 状态（只读）。

        用于"到底滚上去了没有"的取证：返回 `image` 是**从 pod 上读回来的**，
        不是 Deployment 的期望值 —— 两者可以不一致（滚动中、或有人在改别处）。
        """
        return await rollout.get_deployment_status(service=service)

    return get_deployment_status


def _build_probe_service():
    async def probe_service(
        service: Annotated[str, Field(description=_SERVICE_DESC)],
        image: Annotated[
            str,
            Field(description=(
                "刚部署上去的那个镜像引用（与 `rollout_deployment` 用的是同一个）。"
                "本工具**只探正在跑这个镜像的 pod** —— 打旧 pod 得到的 200 什么都不证明。"
            )),
        ],
        path: Annotated[
            str,
            Field(description=(
                "**业务探针**：这次故障打到的那条链路（服务内相对路径，如 "
                "`/quotation?orderId=ORD001`）。**原样取自 plan 的 `verification_probe.path`**，"
                "不要自己拼。**留空 = 只探存活**（合法，用于没有 HTTP 链路的故障）。"
            )),
        ] = "",
        expect: Annotated[
            int,
            Field(description="修好之后该返回的码（取自 plan 的 `verification_probe.expect`）"),
        ] = 0,
        broken_expect: Annotated[
            int,
            Field(description=(
                "**故障态**返回的码（取自 plan 的 `verification_probe.broken_expect`）。"
                "必须与 expect 不同 —— 相同说明这条探针证明不了修复，本工具会直接拒。"
            )),
        ] = 0,
    ) -> dict:
        """对刚部署上去的 pod 打探针（**只读**）：健康层 + 可选的业务链路。

        - **健康层**（路径与端口）来自 **Deployment 自己声明的探针**，不需要你传；
        - **业务层**由 `path`/`expect`/`broken_expect` 给（可留空 ⇒ 只探存活）。

        返回 `passed` / `coverage`（`business` 或 `health_only`）/ 每条探针的结果。
        `passed: false` 时**必须如实上报**，不要因为"部署成功了"就当成验证通过。
        """
        return await probe.probe_service(
            service=service, image=image, path=path, expect=expect, broken_expect=broken_expect
        )

    return probe_service


#: 只读工具（注册时标 readOnlyHint=True —— agent 侧据此**自动 ALLOW**）
READ_ONLY_FACTORIES = {
    "get_deployment_status": _build_get_deployment_status,
    "probe_service": _build_probe_service,
}

#: 写工具（必须显式标 readOnlyHint=False）
#: ⚠️ 标错的后果不是"少一层保护"，是"它无需任何授权就能改线上"——见 tools/__init__.py。
WRITE_FACTORIES = {
    "rollout_deployment": _build_rollout_deployment,
}
