"""把某个 Deployment 滚到指定镜像 —— 本 server 的**全部**业务。

## 三步之内的分工

    minikube image load   → 宿主 store 里的镜像装进节点 containerd（**不经 registry**）
    kubectl set image     → 改 pod template，触发滚动
    kubectl rollout status → 等它滚完
    回读 pod 的镜像        → **"真的滚上去了"的唯一凭据**

## 两个判据（都不是形式）

**① image load 失败就停，不做 set image。**
否则集群拉不到那个 tag，`rollout status` 会白等一整轮超时才报错 —— 而且报的是
`ImagePullBackOff`，离"宿主 store 里没这个镜像"很远。

**② 收尾必须回读 pod 的镜像。**
`rollout status` 只证明"Deployment 的 rollout 完成了"，**不证明"跑着的是我要的镜像"**：
`replicas: 0` 时 `set image` 会"成功"而**没有任何 pod 起来**，`rollout status` 也可能立刻返回。
回读是唯一能抓住它的判据（agentflow §3.3「**声称改了 ≠ 真改了**」的 deploy 版）。

## 失败怎么报：**结果 vs 配置**，分两种

| 情况 | 怎么处理 | 为什么 |
|---|---|---|
| service 不在部署目标表 / 参数非法 / CLI 不存在 | **抛 `AppError`** | 那是**部署错误**，调用方重试也没用 |
| image load 失败 / set image 失败 / 滚动超时 / 回读不符 | **返回 `{success: false, ...}`** | 那是**这次的结果**，调用方要如实上报"没滚上去"（对应 agentflow 的 `deployed: false` → 节点判红） |

**绝不静默降级**：任何一种失败都不会返回 `success: true`。

## 不自动回滚

滚动失败时**不**执行 `kubectl rollout undo`。三个理由：它是又一个不可逆动作而这条链上
每个不可逆动作前面都有门；它会**掩盖失败**（run 报 failed 而线上已被悄悄换回旧版本）；
本仓取向是「宁可红着说失败，不要绿着或悄悄补偿」。
**替代**：把回滚命令写进错误文案 —— 给人信息，不替人做决定。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from deploy_ops_mcp_server.config import get_settings
from deploy_ops_mcp_server.errors import AppError, ErrorCode

logger = logging.getLogger("deploy_ops_mcp_server.rollout")

# 允许的 k8s 对象名/命名空间字符集（RFC 1123 子集）+ 长度上限
# （与 aiops-datasource-mcp-server 的 backends/k8s.py 同一份判据）
_MAX_NAME_LEN = 253

#: 回读时认哪些 phase 算"在跑"。Pending 的 pod 不算 —— 它的镜像字段可能是**上一个**版本的。
_RUNNING_PHASE = "Running"


def _check_name(value: str, field: str) -> str:
    """校验 **k8s 对象名**（namespace / deployment / container）：
    防注入；exec 虽不经 shell，仍拒绝异常字符以便早失败。
    """
    if not value or len(value) > _MAX_NAME_LEN:
        raise AppError(ErrorCode.INVALID_REQUEST, f"{field} 非法（空或过长）：{value!r}")
    if not all(c.isalnum() or c in "-._" for c in value):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{field} 含非法字符（只允许字母数字与 -._）：{value!r}",
        )
    return value


#: 镜像引用的合法形态：必须**以字母数字开头**，其后允许 `-._ / : @`
#: （覆盖 `<registry>:<port>/<path>:<tag>@<digest>` 这些真实写法）。
#:
#: ⚠️ **不能拿 `_check_name` 来校镜像** —— 那个只用给 k8s 对象名，会拒掉 `:`（tag 分隔符），
#: 所有镜像都过不去。实测：第一版就是这么写的，测试当场红了。
#:
#: ⚠️ **必须以字母数字开头**（不是"允许 `-` 但不能只在开头"）：`minikube image load <image>`
#: 把镜像当**独立 argv** 传，一个以 `-` 开头的值会被 minikube 当**旗标**解析 ——
#: 同 §9.7 那条（gh 的 `--title <值>` 会被当旗标，所以改成 `--title=` 单参形式）。
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@\-]*$")


def _check_image(value: str) -> str:
    """校验镜像引用（形态 + **首位不是 `-`**）。"""
    if not value or len(value) > _MAX_NAME_LEN:
        raise AppError(ErrorCode.INVALID_REQUEST, f"image 非法（空或过长）：{value!r}")
    if not _IMAGE_RE.match(value):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"image 形态非法：{value!r}（要求以字母数字开头，其后只允许字母数字与 . _ / : @ -）"
            "—— 以 `-` 开头的值会被 minikube 当成旗标解析。",
        )
    return value


# ----------------------------------------------------------------------
# 子进程封装：三者同一形状（create_subprocess_exec + 超时 + 错误归一）
# ----------------------------------------------------------------------
async def _exec(bin_name: str, args: list[str], *, timeout: float, what: str) -> tuple[int, str, str]:
    """跑一条 CLI 命令，返回 ``(rc, stdout, stderr)``。**不抛** —— 由调用方决定怎么解读。

    缺二进制是**配置错误**（抛），跑起来非零是**结果**（返回给调用方）—— 两者分开，
    因为前者重试无用、后者要如实上报。

    ``stdin=DEVNULL`` 与超时是必须的：CLI 若停在交互式输入上（kubectl 的认证提示、
    minikube 的确认类），没有它们就是**永久挂起**（agentflow §9.6 为一族这样的
    缺陷付过代价：一个 `vi` 把节点挂了 14 分钟）。
    """
    settings = get_settings()
    cmd = [bin_name]
    if bin_name == settings.deploy_ops_kubectl_bin and settings.deploy_ops_kubeconfig:
        cmd += ["--kubeconfig", settings.deploy_ops_kubeconfig]
    cmd += args

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"找不到 {bin_name} —— 请安装，或配置对应的 *_BIN 环境变量。"
            "发布链只在**本地进程**形态可用（详见本模块与 __init__ 的拓扑约束）。",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"{what} 超时（>{timeout}s）已中止"

    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


async def _kubectl(*args: str, timeout: float | None = None) -> tuple[int, str, str]:
    settings = get_settings()
    return await _exec(
        settings.deploy_ops_kubectl_bin, list(args),
        timeout=timeout if timeout is not None else settings.deploy_ops_subprocess_timeout_sec,
        what=f"kubectl {' '.join(args)}",
    )


async def _minikube(*args: str, timeout: float) -> tuple[int, str, str]:
    settings = get_settings()
    return await _exec(
        settings.deploy_ops_minikube_bin, list(args), timeout=timeout,
        what=f"minikube {' '.join(args)}",
    )


def _truncate(text: str) -> str:
    """超长输出截断并**显式标注**（同 datasource 的 http.py 约定）。"""
    limit = get_settings().deploy_ops_max_response_bytes
    if len(text) <= limit:
        return text
    return text[-limit:] + f"\n... (尾部 {limit} bytes，前段已截断)"


# ----------------------------------------------------------------------
# 回读
# ----------------------------------------------------------------------
def _rollback_hint(namespace: str, deployment: str) -> str:
    return (
        f"如需回退：kubectl -n {namespace} rollout undo deploy/{deployment}"
        "（本 server **不会**自动回滚 —— 自动回滚会掩盖失败，见模块 docstring）"
    )


#: docker/podman **展示**镜像时补上的前缀。比较前把它剥掉，让
#: `docker.io/library/order-service:t` 与 `order-service:t` 能对上。
#: 顺序有意义：长前缀先剥（`docker.io/library/` 剥完不该再剥 `library/`）。
_DOCKERHUB_PREFIXES = ("docker.io/library/", "docker.io/", "library/")


def _normalize_ref(ref: str) -> str:
    """把镜像引用折回可比形态。**刻意保守**：认不出来就原样返回。

    认不出来的后果是"比较不上 ⇒ 照常 load" —— 慢 144 秒，但**对**。
    反过来（猜宽了 ⇒ 跳过一次该做的 load）会让集群拉不到镜像，
    症状是 `ImagePullBackOff`，离原因很远。**宁可慢，不可漏。**
    """
    for prefix in _DOCKERHUB_PREFIXES:
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


async def _image_in_node(image: str) -> bool:
    """节点（containerd）里**已经**有这个镜像吗？**判不了就当没有。**

    为什么要问：`minikube image load` 实测 **144 秒**（哪怕镜像早就在节点里），
    而我们的 tag 是**内容寻址**的（`<svc>:<主干 sha12>`）—— 同名即同物，
    已经在节点里的那份**就是**要发的那份，不必再灌一遍。

    三个"判不了"的出口**一律回 False**（于是照常 load）：
      · `minikube image ls` 非零退出
      · 超时（`_exec` 会回 rc=-1）
      · 名字归一化后仍对不上

    这条判据的方向是**故意**的：慢一次可以接受，漏一次不行。
    """
    settings = get_settings()
    rc, out, _err = await _minikube("image", "ls", timeout=settings.deploy_ops_probe_timeout_sec)
    if rc != 0:
        return False
    want = _normalize_ref(image)
    return any(_normalize_ref(ln.strip()) == want for ln in out.splitlines() if ln.strip())


async def _read_running_pods(namespace: str, deployment: str, container: str) -> dict:
    """回读该 Deployment **真正在服务**的 pod（名字 + 镜像）。

    返回 ``{found, pods: [{pod, phase, container, image}], reason}``。
    ``pods`` 是**全部**符合条件的 —— 判"滚上去了没有"要的是它们**都**对，
    不是"随便挑一个对"（见 `rollout_deployment` 第 ④ 步）。

    ## 两条过滤，都是真机踩出来的

    **① 排除 terminating 的 pod。**
    终止中的 pod **仍然报 `phase: Running`**（phase 只反映容器状态，删除是
    `metadata.deletionTimestamp` 的事）。滚动刚完成时，旧 pod 往往还在终止 ——
    不排除它，就会拿**旧镜像**去比对，报一个**假失败**。
    实测（2026-09-28，本机 testbed）：`rollout status` 已报 successfully rolled out、
    新 pod 已在跑新镜像，而回读拿到了那个正在终止的旧 pod ⇒ 误报 `read_back` 失败。

    **② 排除 Pending 的 pod。**
    它还没起来，`image` 字段可能是上一个版本的 —— 拿它比对同样是假结论。
    """
    rc, out, err = await _kubectl("get", "pods", "-n", namespace, "-o", "json",
                                  "-l", f"app={deployment}")
    if rc != 0:
        return {"found": False, "reason": f"读 pod 列表失败：{err.strip()[:300]}"}

    try:
        items = json.loads(out).get("items") or []
    except json.JSONDecodeError as exc:
        return {"found": False, "reason": f"pod 列表不是合法 JSON：{exc}"}

    def _image_of(pod: dict) -> tuple[str, str]:
        containers = (pod.get("spec") or {}).get("containers") or []
        picked = next((c for c in containers if c.get("name") == container), None)
        if picked is None:
            picked = containers[0] if containers else {}
        return picked.get("name") or "", picked.get("image") or ""

    live = []
    for pod in items:
        md, st = pod.get("metadata") or {}, pod.get("status") or {}
        if md.get("deletionTimestamp"):      # ① 终止中 —— 它仍报 Running，别信
            continue
        if st.get("phase") != _RUNNING_PHASE:  # ② 还没起来
            continue
        cname, image = _image_of(pod)
        live.append({"pod": md.get("name"), "phase": st.get("phase"),
                     "container": cname, "image": image})

    if not live:
        return {
            "found": False,
            "reason": (
                f"namespace {namespace} 下没有**在服务**的 pod（共 {len(items)} 个；"
                "已排除终止中与未就绪的）—— rollout 报成功但没有 pod 在跑，"
                "最常见的是 replicas=0"
            ),
        }
    return {"found": True, "pods": live}


# ----------------------------------------------------------------------
# 对外：两个动作
# ----------------------------------------------------------------------
async def get_deployment_status(service: str) -> dict:
    """读该服务当前**在跑的**镜像与 pod 状态（只读）。"""
    settings = get_settings()
    spec = settings.target_for(service)          # 缺条 ⇒ 抛
    ns = _check_name(spec["namespace"], "namespace")
    dep = _check_name(spec["deployment"], "deployment")
    container = _check_name(spec["container"], "container")

    observed = await _read_running_pods(ns, dep, container)
    if not observed.get("found"):
        return {
            "success": True, "service": service, "namespace": ns, "deployment": dep,
            "observed": False, "reason": observed.get("reason"),
            "summary": f"{service}: 读不到在服务的 pod（{observed.get('reason')}）",
        }
    pods = observed["pods"]
    first = pods[0]
    return {
        "success": True, "service": service, "namespace": ns, "deployment": dep,
        "observed": True,
        "pod": first["pod"], "phase": first["phase"],
        "container": first["container"], "image": first["image"],
        # 多副本时把全部都给出 —— 单看第一个会漏掉"有一部分 pod 还是旧的"
        "pods": pods,
        "summary": f"{service}: pod {first['pod']} 在跑 {first['image']}"
                   + (f"（共 {len(pods)} 个在服务）" if len(pods) > 1 else ""),
    }


async def rollout_deployment(service: str, image: str) -> dict:
    """把 ``service`` 滚到 ``image``（**写操作**）。四步，见模块 docstring。"""
    settings = get_settings()
    spec = settings.target_for(service)          # 缺条 ⇒ 抛（不猜默认命名空间）
    ns = _check_name(spec["namespace"], "namespace")
    dep = _check_name(spec["deployment"], "deployment")
    container = _check_name(spec["container"], "container")
    image = (image or "").strip()
    if not image:
        raise AppError(ErrorCode.INVALID_REQUEST, "image 为空 —— 没给镜像就没什么可滚的")
    _check_image(image)

    # ── ① 装进节点（**已经在就跳过**）────────────────────────────────
    # 实测 `minikube image load` 是 **144 秒**（哪怕镜像早就在节点里）；而 tag 是内容
    # 寻址的，"同名即同物" ⇒ 已经在节点里的那份就是要发的那份，重灌一遍纯属浪费。
    reused = await _image_in_node(image)
    rc, out, err = 0, "", ""
    if not reused:
        rc, out, err = await _minikube(
            "image", "load", image, timeout=settings.deploy_ops_image_load_timeout_sec
        )
    # 装失败就**停在这里**：不做 set image。否则集群拉不到那个 tag，
    # rollout status 会白等一整轮超时，而报的是 ImagePullBackOff —— 离原因很远。
    if rc != 0:
        return {
            "success": False, "deployed": False, "service": service, "image": image,
            "stage": "image_load",
            "error": (
                f"minikube image load 失败（rc={rc}）：{(err or out).strip()[:400]}"
                " —— 镜像可能不在**宿主**的 docker/podman store 里（CI 没构建成功？），"
                "或者本 server 与 docker daemon **不在同一台机器**上（见本 server 的拓扑约束）。"
                "已停在第一步、**未改 Deployment**。"
            ),
        }

    # ── ② 触发滚动 ───────────────────────────────────────────────────
    rc, out, err = await _kubectl("set", "image", f"deploy/{dep}",
                                  f"{container}={image}", "-n", ns)
    if rc != 0:
        return {
            "success": False, "deployed": False, "service": service, "image": image,
            "stage": "set_image", "namespace": ns, "deployment": dep,
            "error": f"kubectl set image 失败（rc={rc}）：{(err or out).strip()[:400]}",
        }

    # ── ③ 等滚动完成 ─────────────────────────────────────────────────
    rc, out, err = await _kubectl(
        "rollout", "status", f"deploy/{dep}", "-n", ns,
        f"--timeout={settings.deploy_ops_rollout_timeout_sec}s",
    )
    if rc != 0:
        return {
            "success": False, "deployed": False, "service": service, "image": image,
            "stage": "rollout_status", "namespace": ns, "deployment": dep,
            "error": (
                f"滚动未完成（rc={rc}）：{(err or out).strip()[:400]}\n"
                + _rollback_hint(ns, dep)
            ),
            "log_tail": _truncate(out),
        }

    # ── ④ 回读：**"真的滚上去了"的唯一凭据** ─────────────────────────
    observed = await _read_running_pods(ns, dep, container)
    if not observed.get("found"):
        return {
            "success": False, "deployed": False, "service": service, "image": image,
            "stage": "read_back", "namespace": ns, "deployment": dep,
            "error": (
                "rollout status 报成功，但回读不到在服务的 pod："
                f"{observed.get('reason')}。**没有 pod 在跑就不算部署成功**"
                "（replicas=0 时 set image 会『成功』而什么都没有起来）。\n"
                + _rollback_hint(ns, dep)
            ),
        }

    # **全部在服务的 pod 都要对** —— 只挑一个对的会漏掉"还有一部分是旧的"
    # （多副本滚动中、或旧 pod 尚未终止完）。
    pods = observed["pods"]
    stale = [p for p in pods if p["image"] != image]
    if stale:
        bad = stale[0]
        return {
            "success": False, "deployed": False, "service": service, "image": image,
            "stage": "read_back", "namespace": ns, "deployment": dep,
            "pod": bad["pod"], "observed_image": bad["image"],
            "error": (
                f"回读不符：pod {bad['pod']} 跑的是 {bad['image']!r}，而要求的是 {image!r}"
                + (f"（在服务的 {len(pods)} 个 pod 里有 {len(stale)} 个没跟上）"
                   if len(pods) > 1 else "")
                + "。rollout status 成功 ≠ 跑着的是我要的镜像。\n"
                + _rollback_hint(ns, dep)
            ),
        }

    live = pods[0]
    logger.info("rolled out %s → %s (%d pod(s))", service, image, len(pods))
    return {
        "success": True, "deployed": True,
        "service": service, "image": image, "image_tag": image,
        "namespace": ns, "deployment": dep, "container": container,
        "pod": live["pod"], "phase": live["phase"],
        #: 回读到的值 —— "真的滚上去了"的唯一凭据（`image` 只是**要求**的值）
        "observed_image": live["image"],
        #: ① 是否**跳过**了 `minikube image load`（节点里本来就有那份）—— 省掉 ~144s 的证据
        "image_reused": reused,
        "summary": f"{service} 已滚到 {image}（pod {live['pod']} 在跑 {live['image']}）"
                   + ("（镜像已在节点里，跳过重灌）" if reused else ""),
    }


# ----------------------------------------------------------------------
# 启动自检
# ----------------------------------------------------------------------
async def probe_runtime() -> tuple[bool, str]:
    """探"本 server 能不能干活"。返回 ``(ok, 说明)`` —— **三态里"判不了"也算不 ok**。

    两件事，对应 __init__ 里那条承重约束：

    ① **docker daemon 可达** —— 镜像 build 在它的 store 里，`minikube image load` 要读它。
       这条不成立时本 server 搬不动镜像，而失败会晚到某次 rollout 才出现。
    ② **minikube 可达** —— 集群在不在。

    ⚠️ 探针本身用**短的**超时（`DEPLOY_OPS_PROBE_TIMEOUT_SEC`）：起不来就赶紧报，
    别让人等半分钟。
    """
    settings = get_settings()
    budget = settings.deploy_ops_probe_timeout_sec

    rc, out, err = await _exec(settings.deploy_ops_docker_bin, ["version", "--format", "{{.Server.Version}}"],
                               timeout=budget, what="docker version")
    if rc != 0:
        return False, (
            f"docker daemon 不可达（{settings.deploy_ops_docker_bin} version 失败）："
            f"{(err or out).strip()[:200]}\n"
            "⚠️ 本 server 必须与 docker/podman daemon 跑在**同一台机器**上 —— "
            "镜像构建在它的 store 里，`minikube image load` 要读它（见本仓 __init__ 的拓扑约束）。"
        )

    rc, out, err = await _minikube("status", timeout=budget)
    if rc != 0:
        return False, (
            f"minikube 不可达（minikube status 失败）：{(err or out).strip()[:200]}\n"
            "先 `minikube start` 再起本 server。"
        )

    return True, f"docker + minikube 就绪（rollout 预算 {settings.deploy_ops_rollout_timeout_sec}s）"
