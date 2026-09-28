"""部署后冒烟：对**刚滚上去的那个 pod**打探针，回答"跑起来真的能响应吗"。

与 `rollout.py` 的分工：那个负责**把镜像滚上去**，这个负责**验它跑起来能响应**。
两者共用 `rollout.py` 里的 CLI 封装与 pod 回读（`_kubectl` / `_read_running_pods`）。

## 探针有**两条来源，都不经人手**

| 层 | 内容 | 从哪来 |
|---|---|---|
| **健康层** | 路径 + 端口 | **Deployment 自己的 `readinessProbe.httpGet`**（退 `livenessProbe` / `startupProbe`） |
| **业务层** | 这次故障打到的那条链路 | **调用方传入**（由 agentflow 的 `plan` 节点产出，见 `VERIFY_DEPLOY_NODE_PLAN` D4/D5） |

健康层为什么从 Deployment 读：**那句探针的语义就是"这容器能不能接流量"**，与冒烟同义。
平台再抄一份就是第二个真源，必然漂移。

## 两个判据（都不是形式）

**① 只探「正在跑这个 image 的 pod」。**
打旧 pod 得到的 200 **什么都不证明** —— 而"探到旧 pod"不是理论风险：本机 testbed 的
启动脚本起了 7 个 `svc/` 的 port-forward，其中 order-service 那个在滚动换过 pod 之后
实测**打不通**（`curl` 0.011s 失败）—— 进程还在、服务名没变，**只有 pod 换了**。
三种假绿形态（滚动没完成 / 被回滚了 / 探到另一个旧 pod）都返回 200。

**② forward 自己起、绑那个 pod、在 `finally` 里 kill。**
`port-forward` 是**长驻子进程**，漏 kill 就是每验一次泄漏一个；而它绑着 pod、占着端口。

## 期望码：两层**判据不同**，这是刻意的

- **健康层**：**2xx/3xx 算过** —— k8s 的 readiness 语义就是这个范围，而那条探针是
  Deployment 声明的，我们照它的语义判；
- **业务层**：**精确等于 `expect`** —— 那个码是调用方声明并核对过的
  （order-service：故障态 500 → 修复后 200）。放宽到 2xx 就证明不了修复。

## 不重试、不自动回滚

一过性抖动与"服务真的起不来"混在一起，重试只会把后者也洗成绿。
**宁可红着说失败，不要绿着或悄悄补偿。**
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket

import httpx

from deploy_ops_mcp_server.backends.rollout import (
    _check_image,
    _check_name,
    _kubectl,
    _normalize_ref,
    _read_running_pods,
)
from deploy_ops_mcp_server.config import get_settings
from deploy_ops_mcp_server.errors import AppError, ErrorCode

logger = logging.getLogger("deploy_ops_mcp_server.probe")

#: 哪几个 probe 字段算「探针通过」——k8s readiness 的语义（2xx / 3xx）。
#: **只用于 Deployment 声明的那条**；调用方传的业务探针走精确比对（见模块 docstring）。
_HEALTHY_MIN, _HEALTHY_MAX = 200, 399


def _check_relative_path(value: str, field: str) -> str:
    """业务探针只收**服务内相对路径**。

    挡住"让探针去打别的主机"：必须 `/` 开头，且不含 `://`。
    加上"forward 绑 pod""端口来自 Deployment"，面就收在**被部署的那个服务**上了。
    """
    v = (value or "").strip()
    # `//` 单独拦：它是**协议相对 URL** 的写法（`//host/path`）。我们的 URL 是
    # `http://127.0.0.1:{port}{path}`，所以它打不到别的主机；但收严不花钱，
    # 而没有哪个正常的 API 路径以 `//` 开头。
    if not v.startswith("/") or v.startswith("//") or "://" in v or " " in v:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"{field} 必须是服务内的**相对路径**（以单个 `/` 开头、不含 `://` 与空格）：{value!r}",
        )
    return v


def _validate_business_probe(path: str, expect: int, broken_expect: int) -> dict | None:
    """校验调用方传来的业务探针。**空 path = 不要业务探针**（合法，见 §D6）。

    三条约束（`VERIFY_DEPLOY_NODE_PLAN` D5）里的后两条在这里强校验：

    - `expect` 与 `broken_expect` **都必须给**且**不同** —— 相同就说明这条探针
      **没有鉴别力**（故障态与正常态返回同一个码），配了等于没配；
    - 路径必须是服务内相对路径（`_check_relative_path`）。

    ⭐ 为什么要求 `broken_expect`：它把"这条探针能不能证伪什么"从**判断题**变成
    **声明 + 校验**。而且真返回它时，报错能直接说「这条路径**还是坏的**」，
    而不是笼统的"码不对"。
    """
    if not (path or "").strip():
        return None
    if not isinstance(expect, int) or not isinstance(broken_expect, int):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"业务探针的 expect / broken_expect 必须是整数：expect={expect!r} "
            f"broken_expect={broken_expect!r}",
        )
    if expect == broken_expect:
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            f"这条探针**没有鉴别力**：expect 与 broken_expect 都是 {expect}。"
            "故障态与正常态返回同一个码 ⇒ 它证明不了修复。"
            "换成一条「修好前失败、修好后成功」的路径（如 order-service 的 "
            "/quotation?orderId=ORD001：故障态 500 → 修复后 200）。",
        )
    return {"path": _check_relative_path(path, "probe.path"),
            "expect": expect, "broken_expect": broken_expect}


async def _deployment_health_probe(namespace: str, deployment: str, container: str) -> dict | None:
    """从 **Deployment 自己的探针声明**里读出 `port` 与健康路径。

    读 `readinessProbe`（语义与冒烟最贴）→ 退 `livenessProbe` → 退 `startupProbe`。
    三处都没有 HTTP 探针 ⇒ 返回 **None**（由调用方转成 `deployment_probe_missing` 的失败结果），
    绝不猜一个 8080 —— 猜出来的端口打不通时，报错会指向"服务没起来"。

    ⚠️ 「没有探针声明」**刻意不抛异常**（与 `_check_relative_path` 那类调用方输入错误不同）：
    抛出去的话，这个失败就只存在于**工具的错误信息**里，而节点判红要看 agent 输出的
    `passed` —— 那就把"这次没验成"押在了模型的自觉上。**返回 `passed: false` 才是 fail-closed。**
    """

    rc, out, err = await _kubectl("get", "deploy", deployment, "-n", namespace, "-o", "json")
    if rc != 0:
        raise AppError(
            ErrorCode.TOOL_EXECUTION_ERROR,
            f"读 Deployment {namespace}/{deployment} 失败：{err.strip()[:300]}",
        )
    try:
        d = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AppError(ErrorCode.TOOL_EXECUTION_ERROR, f"Deployment 不是合法 JSON：{exc}") from exc

    containers = ((d.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    picked = next((c for c in containers if c.get("name") == container), None)
    if picked is None:
        raise AppError(
            ErrorCode.CONFIG_ERROR,
            f"Deployment {deployment} 里没有名为 {container!r} 的容器"
            f"（有：{[c.get('name') for c in containers]}）—— 检查 DEPLOY_OPS_TARGETS 的 container 字段",
        )

    for key in ("readinessProbe", "livenessProbe", "startupProbe"):
        http_get = (picked.get(key) or {}).get("httpGet")
        if not http_get:
            continue
        port = http_get.get("port")
        if isinstance(port, str):
            # 具名端口：从容器声明的 ports 里解出实际端口号
            named = next((cp for cp in (picked.get("ports") or []) if cp.get("name") == port), None)
            if not named:
                raise AppError(
                    ErrorCode.CONFIG_ERROR,
                    f"Deployment {deployment} 的 {key}.httpGet.port 用了具名端口 {port!r}，"
                    "但容器上没有同名的 ports 声明 —— 无法解析成端口号",
                )
            port = named.get("containerPort")
        if not isinstance(port, int):
            raise AppError(
                ErrorCode.CONFIG_ERROR,
                f"Deployment {deployment} 的 {key}.httpGet.port 解不出端口号：{http_get.get('port')!r}",
            )
        return {
            "path": http_get.get("path") or "/",
            "port": port,
            "source": f"deployment.{key}",
        }

    return None


def _free_port() -> int:
    """挑一个空闲的本地端口。bind 到 0 让内核分配，读到号就关掉。

    有一点竞态（关掉到 kubectl 用上之间），但窗口是毫秒级，且失败形态是
    `forward_failed` 而不是静默 —— 可接受。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_forward_ready(port: int, timeout: float) -> bool:
    """等 `kubectl port-forward` 可以连上。

    用 **TCP 连一次**判断，而不是去解析它的 stdout —— 后者要处理缓冲与半行，脆。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False


async def _get(path: str, port: int, timeout: float) -> tuple[int | None, str | None, int]:
    """打一条 HTTP 探针，返回 ``(status, error, 耗时ms)``。**不抛** —— 由调用方解读。"""
    url = f"http://127.0.0.1:{port}{path}"
    # ⚠️ `trust_env=False`：**别让 loopback 走代理**。本机 macOS 的系统代理指向一个
    # 没人监听的端口（实测），而 httpx 在 mac 上会读系统代理且**不像 curl 那样跳过 loopback**
    # —— 不关掉的话探针会打到死端口上超时，而报错完全看不出是代理。
    # 同 agentflow 的 `SandboxClient`（它也是 `trust_env=False`）。
    started = asyncio.get_running_loop().time()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.get(url)
        return r.status_code, None, int((asyncio.get_running_loop().time() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - 连接层各种异常统一成"探针失败"
        return None, f"{type(exc).__name__}: {exc!r}", int(
            (asyncio.get_running_loop().time() - started) * 1000
        )


async def probe_service(
    service: str, image: str, path: str = "", expect: int = 0, broken_expect: int = 0
) -> dict:
    """对**正在跑 ``image`` 的那个 pod**打探针（只读）。

    健康层来自 Deployment 自己的声明；业务层由调用方传入（可空 ⇒ 只探健康层，
    `coverage: health_only`，**不判红** —— CPU 打满那类故障本就没有 HTTP 链路）。
    """
    settings = get_settings()
    spec = settings.target_for(service)          # 缺条 ⇒ 抛
    ns = _check_name(spec["namespace"], "namespace")
    dep = _check_name(spec["deployment"], "deployment")
    container = _check_name(spec["container"], "container")
    image = (image or "").strip()
    _check_image(image)
    biz = _validate_business_probe(path, expect, broken_expect)

    # ① 健康层：从 Deployment 自己的探针声明读（端口 + 路径）
    health = await _deployment_health_probe(ns, dep, container)
    if health is None:
        return {
            "passed": False, "stage": "deployment_probe_missing",
            "service": service, "image": image,
            "coverage": "business" if biz else "health_only",
            "namespace": ns, "deployment": dep,
            "error": (
                f"Deployment {dep} 的容器 {container!r} **没有声明任何 HTTP 探针**"
                "（readinessProbe / livenessProbe / startupProbe 都没有 httpGet）"
                "—— 本 server 不猜一个默认端口与路径，**也未发任何 HTTP 请求**。"
                "给该 Deployment 加一条 HTTP 探针再来。"
            ),
        }

    # ② 只认**正在跑这个 image 的**非终止 Running pod
    observed = await _read_running_pods(ns, dep, container)
    if not observed.get("found"):
        return {
            "passed": False, "stage": "pod_not_found", "service": service, "image": image,
            "coverage": "business" if biz else "health_only",
            "error": f"没有可探的 pod：{observed.get('reason')}。**未发任何 HTTP 请求**"
                     " —— 打旧 pod 得到的 200 什么都不证明。",
        }
    want = _normalize_ref(image)
    target = next((p for p in observed["pods"] if _normalize_ref(p["image"] or "") == want), None)
    if target is None:
        return {
            "passed": False, "stage": "pod_not_found", "service": service, "image": image,
            "coverage": "business" if biz else "health_only",
            "observed_images": [p["image"] for p in observed["pods"]],
            "error": f"没有**正在跑 {image}** 的 pod（在服务的："
                     f"{[p['image'] for p in observed['pods']]}）。**未发任何 HTTP 请求** —— "
                     "滚动可能还没完成、或被回滚了。",
        }

    # ③ 起临时 forward（绑**那个** pod），探完在 finally 里 kill
    local_port = _free_port()
    kubectl = settings.deploy_ops_kubectl_bin
    argv = [kubectl]
    if settings.deploy_ops_kubeconfig:
        argv += ["--kubeconfig", settings.deploy_ops_kubeconfig]
    argv += ["port-forward", f"pod/{target['pod']}", f"{local_port}:{health['port']}", "-n", ns]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready = await _wait_forward_ready(local_port, settings.deploy_ops_forward_ready_timeout_sec)
        if not ready:
            return {
                "passed": False, "stage": "forward_failed", "service": service, "image": image,
                "coverage": "business" if biz else "health_only",
                "pod": target["pod"], "namespace": ns, "port": health["port"],
                "error": f"port-forward 在 {settings.deploy_ops_forward_ready_timeout_sec}s 内没起来"
                         f"（pod/{target['pod']} -n {ns}）",
            }

        # ④ 探针：健康层（2xx/3xx）+ 业务层（精确比对）
        wanted = [{"path": health["path"], "expect": None, "source": health["source"]}]
        if biz:
            wanted.append({"path": biz["path"], "expect": biz["expect"],
                           "broken_expect": biz["broken_expect"], "source": "plan"})

        results = []
        for w in wanted:
            status, error, ms = await _get(
                w["path"], local_port, settings.deploy_ops_probe_http_timeout_sec
            )
            if w["expect"] is None:                      # 健康层：k8s 语义
                ok = status is not None and _HEALTHY_MIN <= status <= _HEALTHY_MAX
            else:                                        # 业务层：精确比对
                ok = status == w["expect"]
            item = {**w, "status": status, "ok": ok, "ms": ms, "error": error}
            if error is None and not ok:
                if w["expect"] is None:
                    item["why"] = f"期望 {_HEALTHY_MIN}-{_HEALTHY_MAX}（Deployment 声明的探针语义）"
                elif status == w.get("broken_expect"):
                    item["why"] = f"**这条路径还是坏的**：返回故障态的 {status}"
                else:
                    item["why"] = f"期望 {w['expect']}，实际 {status}"
            results.append(item)

        passed = all(r["ok"] for r in results)
        failed = [r for r in results if not r["ok"]]
        coverage = "business" if biz else "health_only"
        logger.info("probe %s → passed=%s coverage=%s", service, passed, coverage)
        return {
            "passed": passed, "coverage": coverage,
            "service": service, "image": image,
            "namespace": ns, "deployment": dep, "pod": target["pod"],
            "observed_image": target["image"], "port": health["port"],
            "probes": results, "failed": failed,
            "summary": f"{len(results) - len(failed)}/{len(results)} 探针通过"
                       + ("（业务链路已验）" if biz else "（**只验了存活，没验业务链路**）"),
        }
    finally:
        # ⑤ **无论成败都 kill** —— port-forward 是长驻子进程，漏了就是每验一次泄漏一个
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        # 回收它的管道，避免 ResourceWarning 噪声
        with contextlib.suppress(Exception):
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    await stream.read()
