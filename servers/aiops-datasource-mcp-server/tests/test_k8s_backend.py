"""K8s 后端：kubectl 子进程封装（monkeypatch，不依赖真实集群）。"""
from __future__ import annotations

import json

import pytest
from aiops_datasource_mcp_server.backends import k8s
from aiops_datasource_mcp_server.errors import AppError, ErrorCode


@pytest.fixture
def fake_kubectl(monkeypatch):
    """把 _kubectl 换成记录调用的桩；返回 (calls, 设置返回值的函数)。"""
    calls: list[tuple] = []
    responses: dict[str, str] = {}

    async def _fake(*args: str) -> str:
        calls.append(args)
        key = " ".join(args)
        for pattern, payload in responses.items():
            if pattern in key:
                return payload
        return json.dumps({})

    monkeypatch.setattr(k8s, "_kubectl", _fake)
    return calls, responses


async def test_check_infra_rejects_injection(env, fake_kubectl) -> None:
    """namespace/pod 含非法字符一律拒绝（早失败，且防止参数被注入）。"""
    with pytest.raises(AppError) as ei:
        await k8s.check_infra(namespace="order; rm -rf /")
    assert ei.value.code == ErrorCode.INVALID_REQUEST

    with pytest.raises(AppError):
        await k8s.check_infra(namespace="order", pod="$(whoami)")


async def test_check_infra_lists_pods_when_pod_absent(env, fake_kubectl) -> None:
    """不传 pod → 列出该 namespace 全部 pod（比报错更有用）。"""
    calls, responses = fake_kubectl
    responses["get pods"] = json.dumps({"items": [
        {"metadata": {"name": "order-service-abc"},
         "status": {"phase": "Running",
                    "containerStatuses": [{"restartCount": 2}]}},
        {"metadata": {"name": "warranty-service-xyz"},
         "status": {"phase": "Running",
                    "containerStatuses": [{"restartCount": 0}]}},
    ]})

    out = await k8s.check_infra(namespace="order")
    assert out["pod_count"] == 2
    assert out["pods"][0]["restarts"] == 2
    assert "order-service-abc" in str(out["pods"])
    assert calls and "get" in calls[0]


async def test_check_infra_resolves_service_name_to_pod(env, fake_kubectl) -> None:
    """传服务名（非完整 pod 名）→ 按 app 标签匹配，再取详情。"""
    calls, responses = fake_kubectl
    responses["-l app=warranty-service"] = json.dumps(
        {"items": [{"metadata": {"name": "warranty-service-6658df99d4-fdv9m"}}]}
    )
    responses["get pod warranty-service-6658df99d4-fdv9m"] = json.dumps({
        "metadata": {"name": "warranty-service-6658df99d4-fdv9m"},
        "spec": {"nodeName": "minikube"},
        "status": {"phase": "Running", "reasons": None,
                   "containerStatuses": [{"restartCount": 1}]},
    })

    out = await k8s.check_infra(namespace="order", pod="warranty-service")
    assert out["pod"] == "warranty-service-6658df99d4-fdv9m"
    assert out["restarts"] == 1
    assert out["status"] == "Running"


async def test_describe_pod_requires_pod(env, fake_kubectl) -> None:
    with pytest.raises(AppError) as ei:
        await k8s.describe_pod(namespace="order")
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "pod" in ei.value.message


async def test_describe_pod_returns_text(env, fake_kubectl) -> None:
    calls, responses = fake_kubectl
    responses["describe"] = "Name: warranty-service\nEvents:\n  Normal Scheduled"
    out = await k8s.describe_pod(namespace="order", pod="warranty-service-abc")
    assert "Events" in out["describe"]
    assert out["status"] == "described"


async def test_missing_kubectl_is_config_error(env, monkeypatch) -> None:
    """kubectl 不存在 → CONFIG_ERROR（提示安装），而不是裸 FileNotFoundError。"""
    async def _boom(*args: str) -> str:
        raise AppError(ErrorCode.CONFIG_ERROR, "找不到 kubectl")

    monkeypatch.setattr(k8s, "_kubectl", _boom)
    with pytest.raises(AppError) as ei:
        await k8s.check_infra(namespace="order")
    assert ei.value.code == ErrorCode.CONFIG_ERROR
