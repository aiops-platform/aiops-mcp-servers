"""`probe_service` 的行为锁。

本文件锁五类**坏了不会报错**的事：

1. **只探跑着那个 image 的 pod** —— 打旧 pod 得到的 200 什么都不证明
   （三种假绿形态：滚动没完成 / 被回滚了 / 探到另一个旧 pod）；
2. **端口与健康路径从 Deployment 自己声明里读** —— 读不到就报错，不猜 8080；
3. **forward 一定被 kill** —— 它是长驻子进程，漏了就是每验一次泄漏一个；
4. **业务探针的期望码是精确比对**（健康层才是 k8s 的 2xx/3xx 语义）；
5. **没有鉴别力的探针直接拒**（`expect == broken_expect`）。
"""
from __future__ import annotations

import json

import pytest
from deploy_ops_mcp_server.backends import probe, rollout
from deploy_ops_mcp_server.errors import AppError, ErrorCode

IMAGE = "order-service:211eae4e2518"
BIZ_PATH = "/quotation?orderId=ORD001"


def _deployment(*, probe_path="/actuator/health", port=8080, no_probe=False) -> str:
    container: dict = {"name": "order-service", "ports": [{"name": "http", "containerPort": port}]}
    if not no_probe:
        container["readinessProbe"] = {"httpGet": {"path": probe_path, "port": port}}
    return json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})


def _pod(name="order-service-7d9f-abcde", image=IMAGE, *, terminating=False) -> dict:
    md = {"name": name}
    if terminating:
        md["deletionTimestamp"] = "2026-09-28T02:17:10Z"
    return {"metadata": md, "status": {"phase": "Running"},
            "spec": {"containers": [{"name": "order-service", "image": image}]}}


class _FakeProc:
    """`kubectl port-forward` 的替身。**记下有没有被 kill** —— 那是本文件要锁的重点之一。"""

    def __init__(self) -> None:
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


def _stub(monkeypatch, *, deployment=None, pods=None, forward_ready=True,
          statuses=None, http_error=None):
    """桩掉 kubectl / port-forward / HTTP 三层，返回 `(spawned, http_calls)`。

    ``statuses``：路径 → 状态码；``http_error``：路径 → 错误串（模拟连不上）。
    """
    spawned: list[_FakeProc] = []
    http_calls: list[str] = []
    statuses = statuses or {}

    async def fake_kubectl(*args, timeout=None):
        if args[:2] == ("get", "deploy"):
            return 0, deployment if deployment is not None else _deployment(), ""
        if args[:1] == ("get",):
            items = pods if pods is not None else [_pod()]
            return 0, json.dumps({"items": items}), ""
        return 0, "", ""

    # probe 与 rollout 各自持有对 `_kubectl` 的引用（import 进各自命名空间），两处都要换
    monkeypatch.setattr(rollout, "_kubectl", fake_kubectl)
    monkeypatch.setattr(probe, "_kubectl", fake_kubectl)

    async def fake_spawn(*argv, **kw):
        proc = _FakeProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", fake_spawn)

    async def fake_ready(port, timeout):
        return forward_ready

    monkeypatch.setattr(probe, "_wait_forward_ready", fake_ready)

    async def fake_get(path, port, timeout):
        http_calls.append(path)
        if http_error and path in http_error:
            return None, http_error[path], 1
        return statuses.get(path, 200), None, 5

    monkeypatch.setattr(probe, "_get", fake_get)
    return spawned, http_calls


# ── 正常路径与两条来源 ──────────────────────────────────────────────────────


async def test_health_layer_comes_from_the_deployment(env, monkeypatch) -> None:
    """**不给业务探针** ⇒ 只探健康层，路径与端口**从 Deployment 读**。

    这条同时锁两件事：端口不是写死的 8080（换个端口也要能读到），
    以及 `coverage: health_only` **如实标注**（CPU 打满那类故障没有 HTTP 链路，
    那是合法的 —— 但必须**看得见**这次只验了存活）。
    """
    _spawned, calls = _stub(monkeypatch, deployment=_deployment(probe_path="/healthz", port=9999),
                            statuses={"/healthz": 200})
    out = await probe.probe_service("order-service", IMAGE)

    assert out["passed"] is True and out["coverage"] == "health_only"
    assert calls == ["/healthz"], calls
    assert out["port"] == 9999 and out["probes"][0]["source"] == "deployment.readinessProbe"
    assert "只验了存活" in out["summary"]


async def test_business_probe_from_calller_and_health_from_deployment(env, monkeypatch) -> None:
    """给了业务探针 ⇒ 两条都探；来源分别标 `deployment.*` 与 `plan`。"""
    _spawned, calls = _stub(monkeypatch, statuses={"/actuator/health": 200, BIZ_PATH: 200})
    out = await probe.probe_service("order-service", IMAGE, BIZ_PATH, 200, 500)

    assert out["passed"] is True and out["coverage"] == "business"
    assert calls == ["/actuator/health", BIZ_PATH]
    assert [p["source"] for p in out["probes"]] == ["deployment.readinessProbe", "plan"]
    assert out["observed_image"] == IMAGE and out["pod"] == "order-service-7d9f-abcde"
    assert "业务链路已验" in out["summary"]


async def test_health_layer_accepts_k8s_success_range(env, monkeypatch) -> None:
    """健康层按 **k8s 的语义**判（2xx/3xx）—— 那条探针是 Deployment 声明的，照它的语义判。"""
    _stub(monkeypatch, statuses={"/actuator/health": 302})
    out = await probe.probe_service("order-service", IMAGE)
    assert out["passed"] is True


# ── 只探跑着那个 image 的 pod（本文件的重点）────────────────────────────────


async def test_no_pod_at_all_never_sends_http(env, monkeypatch) -> None:
    """没有可探的 pod ⇒ `pod_not_found`，**一次 HTTP 都不发**。"""
    spawned, calls = _stub(monkeypatch, pods=[])
    out = await probe.probe_service("order-service", IMAGE)

    assert out["passed"] is False and out["stage"] == "pod_not_found"
    assert calls == [] and spawned == [], "连 forward 都不该起"


async def test_pod_running_a_different_image_is_not_probed(env, monkeypatch) -> None:
    """pod 在跑**别的** image ⇒ 同样不发请求。

    ⚠️ 这条与上一条是同一判据的正反两面，也是本节点最核心的一条：
    打旧 pod 得到的 200 **什么都不证明** —— 本机 testbed 那个绑 `svc/order-service` 的
    port-forward 在滚动换过 pod 之后就打不通了（`curl` 0.011s 失败），而它看上去一切正常。
    """
    spawned, calls = _stub(monkeypatch, pods=[_pod("old", "order-service:latest")])
    out = await probe.probe_service("order-service", IMAGE)

    assert out["passed"] is False and out["stage"] == "pod_not_found"
    assert out["observed_images"] == ["order-service:latest"]
    assert calls == [] and spawned == []


async def test_terminating_pod_is_not_probed(env, monkeypatch) -> None:
    """终止中的 pod 仍报 Running（`rollout` 那边踩过的同一个坑）⇒ 不算可探。"""
    _stub(monkeypatch, pods=[_pod(terminating=True)])
    out = await probe.probe_service("order-service", IMAGE)
    assert out["passed"] is False and out["stage"] == "pod_not_found"


async def test_dockerhub_normalized_image_matches(env, monkeypatch) -> None:
    """节点/Deployment 里是 `docker.io/library/<x>`、调用方给短名 ⇒ 要认成同一个。"""
    _stub(monkeypatch, pods=[_pod(image="docker.io/library/order-service:211eae4e2518")])
    out = await probe.probe_service("order-service", IMAGE)
    assert out["passed"] is True


# ── Deployment 探针读不到 ──────────────────────────────────────────────────


async def test_deployment_without_http_probe_fails_closed(env, monkeypatch) -> None:
    """Deployment **没有声明任何 HTTP 探针** ⇒ `deployment_probe_missing`，**不猜一个 8080**。

    ⚠️ 判据是 `passed: false` 的**返回值**，不是抛异常：抛出去的话这个失败只活在
    工具的错误信息里，而节点判红读的是 agent 输出的 `passed` —— 那等于把"这次没验成"
    押在模型自觉上。同理它**一次 HTTP 都不该发**（连 forward 都不起）。
    """
    spawned, calls = _stub(monkeypatch, deployment=_deployment(no_probe=True))
    out = await probe.probe_service("order-service", IMAGE)

    assert out["passed"] is False and out["stage"] == "deployment_probe_missing"
    assert calls == [] and spawned == [], "连 forward 都不该起"
    assert "没有声明任何 HTTP 探针" in out["error"]


async def test_deployment_named_port_is_resolved(env, monkeypatch) -> None:
    """`httpGet.port` 用具名端口时要能从容器的 ports 里解出端口号。"""
    dep = json.dumps({"spec": {"template": {"spec": {"containers": [{
        "name": "order-service", "ports": [{"name": "http", "containerPort": 8080}],
        "readinessProbe": {"httpGet": {"path": "/actuator/health", "port": "http"}}}]}}}})
    _stub(monkeypatch, deployment=dep)
    out = await probe.probe_service("order-service", IMAGE)
    assert out["passed"] is True and out["port"] == 8080


# ── 探针结果 ────────────────────────────────────────────────────────────────


async def test_broken_expect_says_the_path_is_still_broken(env, monkeypatch) -> None:
    """业务探针返回 `broken_expect` ⇒ 报错文案要说「**这条路径还是坏的**」。"""
    _stub(monkeypatch, statuses={"/actuator/health": 200, BIZ_PATH: 500})
    out = await probe.probe_service("order-service", IMAGE, BIZ_PATH, 200, 500)

    assert out["passed"] is False
    bad = out["failed"][0]
    assert bad["status"] == 500 and "还是坏的" in bad["why"]


async def test_all_probes_still_run_when_the_first_fails(env, monkeypatch) -> None:
    """一条不过不影响其余照跑（不然只看到第一个问题）。"""
    _spawned, calls = _stub(monkeypatch, statuses={"/actuator/health": 500, BIZ_PATH: 200})
    out = await probe.probe_service("order-service", IMAGE, BIZ_PATH, 200, 500)

    assert out["passed"] is False
    assert calls == ["/actuator/health", BIZ_PATH]
    assert len(out["failed"]) == 1 and out["failed"][0]["path"] == "/actuator/health"


async def test_business_probe_is_exact_not_2xx(env, monkeypatch) -> None:
    """业务层是**精确比对**：期望 200、得 302 ⇒ **不过**（健康层才用 2xx/3xx）。"""
    _stub(monkeypatch, statuses={"/actuator/health": 200, BIZ_PATH: 302})
    out = await probe.probe_service("order-service", IMAGE, BIZ_PATH, 200, 500)
    assert out["passed"] is False


async def test_connection_error_is_a_failed_probe(env, monkeypatch) -> None:
    """连不上（不是码不对）⇒ 也算探针失败，且带上异常类型名。"""
    _stub(monkeypatch, http_error={"/actuator/health": "ConnectError: ConnectionRefusedError(61)"})
    out = await probe.probe_service("order-service", IMAGE)
    assert out["passed"] is False
    assert "ConnectError" in out["failed"][0]["error"]


# ── forward 的起与 kill（唯一的进程泄漏面）────────────────────────────────


async def test_forward_is_killed_on_success(env, monkeypatch) -> None:
    """成功路径也要 kill —— `port-forward` 是长驻子进程。"""
    spawned, _ = _stub(monkeypatch)
    await probe.probe_service("order-service", IMAGE)
    assert len(spawned) == 1 and spawned[0].killed is True


async def test_forward_is_killed_when_it_never_becomes_ready(env, monkeypatch) -> None:
    """forward 起不来 ⇒ `forward_failed`，而**进程仍要被 kill**。"""
    spawned, calls = _stub(monkeypatch, forward_ready=False)
    out = await probe.probe_service("order-service", IMAGE)

    assert out["passed"] is False and out["stage"] == "forward_failed"
    assert calls == [], "forward 没起来就不该发请求"
    assert spawned[0].killed is True


async def test_forward_is_killed_when_a_probe_raises(env, monkeypatch) -> None:
    """探针途中抛异常 ⇒ `finally` 仍然 kill。"""
    spawned, _ = _stub(monkeypatch)

    async def boom(path, port, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "_get", boom)
    with pytest.raises(RuntimeError):
        await probe.probe_service("order-service", IMAGE)
    assert spawned[0].killed is True


# ── 输入校验 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["http://evil/x", "x/y", "//evil", "/a b"])
async def test_business_path_must_be_a_relative_path(env, bad) -> None:
    """业务路径必须是**服务内相对路径**（`/` 开头、不含 `://` 与空格）—— 挡住打别的主机。"""
    with pytest.raises(AppError) as ei:
        await probe.probe_service("order-service", IMAGE, bad, 200, 500)
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "相对路径" in ei.value.message


async def test_probe_without_discriminating_power_is_rejected(env) -> None:
    """`expect == broken_expect` ⇒ **直接拒**（这条探针证明不了修复）。"""
    with pytest.raises(AppError) as ei:
        await probe.probe_service("order-service", IMAGE, BIZ_PATH, 200, 200)
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "没有鉴别力" in ei.value.message
