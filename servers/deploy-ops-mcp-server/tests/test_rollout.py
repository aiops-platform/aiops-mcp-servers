"""`rollout_deployment` / `get_deployment_status` 的行为锁。

本文件锁四类**坏了不会报错**的事：

1. **步骤顺序与短路** —— `image load` 失败就**不许** `set image`（否则集群拉不到镜像，
   `rollout status` 白等一整轮超时，而报的是 `ImagePullBackOff` —— 离原因很远）；
2. **回读是承重的** —— `rollout status` 成功 ≠ 跑着的是我要的镜像
   （`replicas=0` 时 `set image` 会"成功"而没有任何 pod 起来）；
3. **失败必须可辨** —— `stage` 指明卡在哪一步，超时/滚动失败的文案里**带回滚命令**；
4. **配置 fail-closed** —— 服务不在目标表里就报错，不猜一个默认命名空间。
"""
from __future__ import annotations

import json

import pytest

from deploy_ops_mcp_server import config as cfg
from deploy_ops_mcp_server.backends import rollout
from deploy_ops_mcp_server.errors import AppError, ErrorCode

IMAGE = "order-service:211eae4e2518"
RUNNING_POD = {
    "metadata": {"name": "order-service-7d9f-abcde"},
    "status": {"phase": "Running"},
    "spec": {"containers": [{"name": "order-service", "image": IMAGE}]},
}


def _stub(monkeypatch, *, image_load=(0, "", ""), set_image=(0, "", ""),
          rollout_status=(0, "deployment rolled out", ""), pods=None,
          node_images=""):
    """桩掉两个 CLI 封装，记录调用序列。

    按 **argv 内容**分派而不是按调用顺序：顺序断言由 `calls` 单独做，
    两件事分开，测试红了能立刻看出是"调错了"还是"顺序错了"。

    ``node_images``：`minikube image ls` 的输出。**默认空** ⇒ `_image_in_node` 回 False
    ⇒ 照常走 `image load`（这是既有用例期望的路径）。
    """
    calls: list[tuple[str, tuple]] = []

    async def fake_minikube(*args, timeout):
        calls.append(("minikube", args))
        if args[:2] == ("image", "ls"):
            return 0, node_images, ""
        return image_load

    async def fake_kubectl(*args, timeout=None):
        calls.append(("kubectl", args))
        if args[:2] == ("set", "image"):
            return set_image
        if args[:2] == ("rollout", "status"):
            return rollout_status
        if args[:1] == ("get",):
            items = pods if pods is not None else [RUNNING_POD]
            return 0, json.dumps({"items": items}), ""
        return 0, "", ""

    monkeypatch.setattr(rollout, "_minikube", fake_minikube)
    monkeypatch.setattr(rollout, "_kubectl", fake_kubectl)
    return calls


# ── 配置 fail-closed ────────────────────────────────────────────────────────


async def test_unknown_service_fails_closed(env) -> None:
    """服务不在部署目标表里 → **抛错**，不猜一个默认命名空间。

    这条挡的是"滚到别的命名空间去"：那种失败是**滚成功了但滚错地方**，
    比报错难查得多。
    """
    with pytest.raises(AppError) as ei:
        await rollout.rollout_deployment(service="no-such-service", image=IMAGE)
    assert ei.value.code == ErrorCode.CONFIG_ERROR
    assert "no-such-service" in ei.value.message


async def test_config_rejects_subprocess_timeout_below_rollout_timeout(
    monkeypatch, clear_settings_cache
) -> None:
    """子进程墙钟 ≤ kubectl 的等待预算 → **构造时就报错**（不是跑到一半才现形）。

    小的那个会静默覆盖大的：滚动慢的时候我们会先把它杀掉，而报错说"命令超时"，
    分不清「滚动慢」与「被我们砍了」。
    """
    monkeypatch.setenv("DEPLOY_OPS_ROLLOUT_TIMEOUT_SEC", "300")
    monkeypatch.setenv("DEPLOY_OPS_SUBPROCESS_TIMEOUT_SEC", "300")
    with pytest.raises(Exception) as ei:
        cfg.Settings()
    assert "必须大于" in str(ei.value)


async def test_config_rejects_target_missing_field(monkeypatch, clear_settings_cache) -> None:
    """目标表条目缺字段 → 启动期就拦住（不是第一次调用时）。"""
    monkeypatch.setenv("DEPLOY_OPS_TARGETS", json.dumps({"svc": {"namespace": "order"}}))
    with pytest.raises(Exception) as ei:
        cfg.Settings()
    assert "缺字段" in str(ei.value)


@pytest.mark.parametrize("bad", ["--force", "-x", "", "a b", "img;rm -rf /"])
async def test_image_must_not_look_like_a_flag_or_carry_spaces(env, monkeypatch, bad) -> None:
    """镜像形态非法 → **抛错**，而不是把它当 argv 传给 minikube。

    ⚠️ 这条挡的是**旗标注入**：`minikube image load <image>` 把镜像当**独立 argv** 传，
    一个以 `-` 开头的值会被 minikube 当成**旗标**解析（同 §9.7 那条 gh `--title <值>`
    被当旗标的坑）。空格同理 —— 虽然不经 shell，但一个带空格的"镜像"绝不是我们要的。

    （`_check_name` 不能用来校镜像：它只允许字母数字与 `-._`，会拒掉 tag 的 `:`。）
    """
    _stub(monkeypatch)
    with pytest.raises(AppError) as ei:
        await rollout.rollout_deployment("order-service", bad)
    assert ei.value.code == ErrorCode.INVALID_REQUEST
    assert "image" in ei.value.message


@pytest.mark.parametrize("good", [
    "order-service:211eae4e2518",
    "registry.internal:5000/team/order-service:211eae4e2518",
    "order-service@sha256:abc123",
])
async def test_image_accepts_real_reference_forms(env, monkeypatch, good) -> None:
    """真实的镜像引用形态都要放行（含 registry 前缀、端口、digest）。

    ⚠️ 这一条与上一条**成对**：只锁"拒掉坏的"会让人写出一个把所有镜像都拒掉的校验
    （本文件第一版就是这么挂的 —— 拿 `_check_name` 校镜像，`:` 过不去）。
    """
    _stub(monkeypatch, pods=[{
        "metadata": {"name": "p"}, "status": {"phase": "Running"},
        "spec": {"containers": [{"name": "order-service", "image": good}]},
    }])
    out = await rollout.rollout_deployment("order-service", good)
    assert out["success"] is True and out["observed_image"] == good


# ── 正常路径 ────────────────────────────────────────────────────────────────


async def test_happy_path_runs_the_four_steps_in_order(env, monkeypatch) -> None:
    """四步顺序 + 回读相符时 `deployed=True`、`observed_image` 是**读回来的值**。"""
    calls = _stub(monkeypatch)
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is True and out["deployed"] is True
    assert out["observed_image"] == IMAGE
    assert out["pod"] == "order-service-7d9f-abcde"
    assert out["namespace"] == "order" and out["deployment"] == "order-service"

    sequence = [c[1][:2] for c in calls]
    assert sequence == [
        ("image", "ls"),          # ① 先问节点里有没有（有就跳过 load）
        ("image", "load"),        # ① 没有 → 装进节点
        ("set", "image"),         # ② 触发滚动
        ("rollout", "status"),    # ③ 等它滚完
        ("get", "pods"),          # ④ 回读
    ], sequence
    assert out["image_reused"] is False
    # ② 必须是 `set image deploy/<d> <container>=<image>`
    set_args = next(c[1] for c in calls if c[1][:2] == ("set", "image"))
    assert set_args[2] == "deploy/order-service"
    assert set_args[3] == f"order-service={IMAGE}"
    # ③ 的等待预算来自配置（与子进程墙钟是两回事）
    status_args = next(c[1] for c in calls if c[1][:2] == ("rollout", "status"))
    assert "--timeout=30s" in status_args


# ── 短路与失败姿态 ──────────────────────────────────────────────────────────


# ── ① 的跳过优化（省 144 秒），与它的**安全方向** ─────────────────────────


async def test_image_already_in_node_skips_the_load(env, monkeypatch) -> None:
    """镜像已在节点里 ⇒ **跳过 `minikube image load`**（实测那一步 144 秒）。

    tag 是**内容寻址**的（`<svc>:<主干 sha12>`）—— 同名即同物，已经在节点里的那份
    就是要发的那份，重灌一遍纯属浪费。
    """
    calls = _stub(monkeypatch, node_images="docker.io/library/order-service:211eae4e2518\n")
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is True and out["deployed"] is True
    assert out["image_reused"] is True
    assert ("minikube", ("image", "load")) not in calls, "已在节点里却还是灌了一遍"
    assert [c[1][:2] for c in calls] == [("image", "ls"), ("set", "image"),
                                         ("rollout", "status"), ("get", "pods")]


async def test_dockerhub_normalized_name_counts_as_present(env, monkeypatch) -> None:
    """`docker.io/library/<x>:<t>` 与 `<x>:<t>` 要认成同一个。

    节点列出来的是**补全后的全名**（实测 `docker.io/library/order-service:…`），
    而调用方给的是短名 —— 不折一下，这条优化永远不会生效（每次都白灌 144 秒）。
    """
    _stub(monkeypatch, node_images="docker.io/library/order-service:211eae4e2518\n")
    out = await rollout.rollout_deployment("order-service", IMAGE)
    assert out["image_reused"] is True


@pytest.mark.parametrize("listing", [
    "",                                                 # 空列表
    "docker.io/library/order-service:other-tag\n",       # 同仓不同 tag
    "docker.io/library/other-service:211eae4e2518\n",    # 同 tag 不同仓
])
async def test_a_different_image_is_not_treated_as_present(env, monkeypatch, listing) -> None:
    """**不匹配就得照常 load** —— 这条与上面两条成对，防止把判据写宽。

    判宽了的后果不是"慢"，是**跳过一次该做的 load** ⇒ 集群拉不到镜像 ⇒
    `ImagePullBackOff`，而报错离原因很远。
    """
    _stub(monkeypatch, node_images=listing)
    out = await rollout.rollout_deployment("order-service", IMAGE)
    assert out["image_reused"] is False
    assert out["success"] is True   # 照常灌、照常滚，只是没省下那 144 秒


async def test_image_listing_failure_falls_back_to_loading(env, monkeypatch) -> None:
    """`minikube image ls` 失败 / 超时 ⇒ **当"没有"处理，照常 load**。

    这条锁的是判据的**方向**：判不了时必须回退到"灌一遍"（慢但正确），
    不能回退到"跳过"（快但可能漏）。
    """
    calls: list[tuple[str, tuple]] = []

    async def fake_minikube(*args, timeout):
        calls.append(("minikube", args))
        if args[:2] == ("image", "ls"):
            return 1, "", "error: minikube unreachable"   # ← 查不了
        return 0, "", ""

    async def fake_kubectl(*args, timeout=None):
        calls.append(("kubectl", args))
        if args[:1] == ("get",):
            return 0, json.dumps({"items": [RUNNING_POD]}), ""
        return 0, "", ""

    monkeypatch.setattr(rollout, "_minikube", fake_minikube)
    monkeypatch.setattr(rollout, "_kubectl", fake_kubectl)

    out = await rollout.rollout_deployment("order-service", IMAGE)
    assert out["image_reused"] is False
    assert any(c[0] == "minikube" and c[1][:2] == ("image", "load") for c in calls), \
        "查不了时必须照常灌，不能跳过"


async def test_image_load_failure_stops_before_set_image(env, monkeypatch) -> None:
    """① 失败 ⇒ **停在那里，绝不 set image**。

    否则集群拉不到那个 tag，`rollout status` 会白等一整轮超时，而报的是
    `ImagePullBackOff` —— 离"宿主 store 里没这个镜像"很远。
    """
    calls = _stub(monkeypatch, image_load=(1, "", "Error: no such image"))
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is False and out["deployed"] is False
    assert out["stage"] == "image_load"
    assert [c[1][:2] for c in calls] == [("image", "ls"), ("image", "load")], \
        "不该走到 set image"
    assert "未改 Deployment" in out["error"]
    # 错误要指向两个真实原因：store 里没有 / 与 docker daemon 不同机
    assert "docker daemon" in out["error"]


async def test_rollout_status_failure_carries_the_rollback_command(env, monkeypatch) -> None:
    """③ 失败 ⇒ `stage=rollout_status`，且文案里**带回滚命令**。

    本 server **不做**自动回滚（自动回滚会掩盖失败），所以必须给人一条能直接粘贴的命令。
    """
    out = await _stub_and_fail(monkeypatch, rollout_status=(1, "", "error: timed out"))

    assert out["stage"] == "rollout_status"
    assert "kubectl -n order rollout undo deploy/order-service" in out["error"]
    assert "不会**自动回滚**" in out["error"] or "自动回滚" in out["error"]


async def test_set_image_failure_is_reported(env, monkeypatch) -> None:
    out = await _stub_and_fail(monkeypatch, set_image=(1, "", "error: not found"))
    assert out["stage"] == "set_image"


async def _stub_and_fail(monkeypatch, **kw) -> dict:
    _stub(monkeypatch, **kw)
    return await rollout.rollout_deployment("order-service", IMAGE)


# ── 回读：本文件最重要的一组 ────────────────────────────────────────────────


async def test_read_back_mismatch_is_a_failure(env, monkeypatch) -> None:
    """④ 回读到的镜像 ≠ 要求的镜像 ⇒ **失败**。

    这是「声称改了 ≠ 真改了」的 deploy 版：`rollout status` 只证明"Deployment 的
    rollout 完成了"，不证明"跑着的是我要的镜像"。
    """
    wrong = dict(RUNNING_POD)
    wrong["spec"] = {"containers": [{"name": "order-service", "image": "order-service:old"}]}
    _stub(monkeypatch, pods=[wrong])
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is False and out["deployed"] is False
    assert out["stage"] == "read_back"
    assert out["observed_image"] == "order-service:old"
    assert IMAGE in out["error"] and "order-service:old" in out["error"]


def _pod(name: str, image: str, *, phase: str = "Running", terminating: bool = False) -> dict:
    md = {"name": name}
    if terminating:
        md["deletionTimestamp"] = "2026-09-28T02:17:10Z"
    return {"metadata": md, "status": {"phase": phase},
            "spec": {"containers": [{"name": "order-service", "image": image}]}}


async def test_terminating_old_pod_is_not_mistaken_for_the_deployed_one(env, monkeypatch) -> None:
    """★ **真机踩出来的回归**：终止中的旧 pod **仍然报 `phase: Running`**。

    实测（2026-09-28，本机 testbed）：滚动其实**成功了** —— 新 RS 就绪、新 pod 在跑新镜像、
    `rollout status` 报 `successfully rolled out`；而回读拿到的是那个**正在终止的旧 pod**
    （它 phase 还是 Running），于是报了一个**假失败**。

    判据：**终止 ≠ 在服务**。过滤靠 `metadata.deletionTimestamp`，不是 phase。
    """
    _stub(monkeypatch, pods=[
        _pod("order-service-old-terminating", "order-service:latest", terminating=True),
        _pod("order-service-new", IMAGE),
    ])
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is True and out["deployed"] is True
    assert out["pod"] == "order-service-new"
    assert out["observed_image"] == IMAGE


async def test_one_stale_live_pod_is_still_a_failure(env, monkeypatch) -> None:
    """但**在服务的** pod 只要有一个没跟上，就必须失败（多副本滚动中）。

    与上一条成对：不能为了躲开"终止中的旧 pod"就把判据放宽成"随便挑一个对的" ——
    那会让"三个副本里两个还是旧的"这种半截状态报成功。
    """
    _stub(monkeypatch, pods=[
        _pod("order-service-new", IMAGE),
        _pod("order-service-stale", "order-service:latest"),
    ])
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is False and out["stage"] == "read_back"
    assert out["observed_image"] == "order-service:latest"
    assert "没跟上" in out["error"]


async def test_read_back_without_running_pod_is_a_failure(env, monkeypatch) -> None:
    """④ 没有 Running 的 pod ⇒ **失败**。

    `replicas=0` 时 `set image` 会"成功"、`rollout status` 也可能立刻返回 ——
    没有 pod 在跑就不算部署成功。这条是那一步唯一的判据。
    """
    pending = {"metadata": {"name": "p"}, "status": {"phase": "Pending"},
               "spec": {"containers": [{"name": "order-service", "image": IMAGE}]}}
    _stub(monkeypatch, pods=[pending])
    out = await rollout.rollout_deployment("order-service", IMAGE)

    assert out["success"] is False and out["stage"] == "read_back"
    assert "没有**在服务**的 pod" in out["error"]


async def test_get_deployment_status_reads_the_running_pod(env, monkeypatch) -> None:
    """只读工具：返回的是**从 pod 上读回来**的镜像，不是 Deployment 的期望值。"""
    _stub(monkeypatch)
    out = await rollout.get_deployment_status("order-service")

    assert out["success"] is True and out["observed"] is True
    assert out["image"] == IMAGE
    assert out["pod"] == "order-service-7d9f-abcde"
