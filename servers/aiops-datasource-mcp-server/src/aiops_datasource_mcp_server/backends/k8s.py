"""Kubernetes 后端：经 kubectl 子进程读取 Pod 状态与详情。

**这两个工具没有时间参数**——查的是集群**当前状态**（design-v5.5 §4）。
"查询目标"即 namespace / pod。

安全约定：
- 命令**白名单**（只 get/describe pod，不提供 apply/delete/exec 等写操作）；
- ``namespace`` / ``pod`` 做字符校验（防参数注入到 shell）；
- 走 ``create_subprocess_exec``（非 shell=True）——参数不经过 shell 解析。
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiops_datasource_mcp_server.config import get_settings
from aiops_datasource_mcp_server.errors import AppError, ErrorCode

logger = logging.getLogger("aiops_datasource_mcp_server.k8s")

# 允许的 k8s 对象名/命名空间字符集（RFC 1123 子集）+ 长度上限
_MAX_NAME_LEN = 253


def _check_name(value: str, field: str) -> str:
    """校验 k8s 名称（防注入；exec 虽不经 shell，但仍拒绝异常字符以便早失败）。"""
    if not value or len(value) > _MAX_NAME_LEN:
        raise AppError(ErrorCode.INVALID_REQUEST, f"{field} 非法（空或过长）：{value!r}")
    if not all(c.isalnum() or c in "-._" for c in value):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{field} 含非法字符（只允许字母数字与 -._）：{value!r}",
        )
    return value


def _is_full_pod_name(name: str) -> bool:
    """判断是否已是被截断过的完整 pod 名（如 order-service-76f5f654dd-mpkz8）。

    k8s 生成的 pod 名含多个 `-`（deployment hash + 随机后缀）；粗略以 `-` 数量判断。
    """
    return bool(name) and name.count("-") >= 4


async def _kubectl(*args: str) -> str:
    """执行 kubectl 并返回 stdout；失败归一为 AppError（不抛裸异常）。"""
    settings = get_settings()
    cmd = [settings.datasource_kubectl_bin]
    if settings.datasource_kubeconfig:
        cmd += ["--kubeconfig", settings.datasource_kubeconfig]
    cmd += list(args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"找不到 kubectl（{settings.datasource_kubectl_bin}）——请安装或配置 "
            f"DATASOURCE_KUBECTL_BIN",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.datasource_request_timeout_sec
        )
    except TimeoutError as exc:
        proc.kill()
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR,
            f"kubectl 超时（{settings.datasource_request_timeout_sec}s）：{' '.join(args)}",
        ) from exc

    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[:500]
        logger.warning("kubectl failed args=%s err=%s", args, err)
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR,
            f"kubectl {' '.join(args)} 失败：{err}",
        )

    text = stdout.decode("utf-8", "replace")
    limit = settings.datasource_max_response_bytes
    if len(text) > limit:
        # 截断显式可见（同 http.py 的约定）
        text = text[:limit] + f"\n... (truncated at {limit} bytes, please narrow the scope)"
    return text


async def check_infra(namespace: str | None = None, pod: str | None = None) -> dict:
    """查询 Pod 状态（pod 为空时列出该 namespace 全部 pod）。"""
    settings = get_settings()
    ns = _check_name(namespace or settings.datasource_k8s_namespace, "namespace")

    if pod:
        _check_name(pod, "pod")
        if _is_full_pod_name(pod):
            spec = pod
        else:
            # 传的是服务名/前缀 → 按 app 标签找第一个匹配 pod
            out = await _kubectl("get", "pods", "-n", ns, "-o", "json", "-l", f"app={pod}")
            items = json.loads(out).get("items") or []
            spec = items[0]["metadata"]["name"] if items else ""
    else:
        spec = ""

    if not spec:
        # 未指定 pod：列该 namespace 的 pod 概览（比报错更有用）
        out = await _kubectl("get", "pods", "-n", ns, "-o", "json")
        items = json.loads(out).get("items") or []
        pods = []
        for it in items:
            md, st = it.get("metadata", {}), it.get("status", {})
            restarts = sum(
                cs.get("restartCount", 0) for cs in (st.get("containerStatuses") or [])
            )
            pods.append({
                "pod": md.get("name"),
                "status": st.get("phase"),
                "restarts": restarts,
                "reason": st.get("reason"),
            })
        return {
            "namespace": ns,
            "pod_count": len(pods),
            "pods": pods,
            "summary": f"namespace {ns} 下 {len(pods)} 个 pod"
            + ("（含异常）" if any(p["restarts"] for p in pods) else ""),
        }

    d = json.loads(await _kubectl("get", "pod", spec, "-n", ns, "-o", "json"))
    md, st = d.get("metadata", {}), d.get("status", {})
    restarts = sum(cs.get("restartCount", 0) for cs in (st.get("containerStatuses") or []))
    conditions = [
        {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
        for c in (st.get("conditions") or [])
    ]
    return {
        "namespace": ns,
        "pod": md.get("name"),
        "status": st.get("phase"),
        "restarts": restarts,
        "reason": st.get("reason"),
        "node": d.get("spec", {}).get("nodeName"),
        "conditions": conditions,
        "summary": f"pod {md.get('name')} status={st.get('phase')} restarts={restarts}",
    }


async def describe_pod(namespace: str | None = None, pod: str | None = None) -> dict:
    """查看 Pod 详情（describe，含事件与资源水位）。"""
    settings = get_settings()
    ns = _check_name(namespace or settings.datasource_k8s_namespace, "namespace")
    if not pod:
        raise AppError(ErrorCode.INVALID_REQUEST, "describe_pod 需要 pod 参数")
    _check_name(pod, "pod")

    out = await _kubectl("describe", "pod", pod, "-n", ns)
    return {
        "namespace": ns,
        "pod": pod,
        "status": "described",
        "describe": out[:8000],
        "summary": f"pod {pod} 的 describe 输出（{len(out)} 字符）",
    }
