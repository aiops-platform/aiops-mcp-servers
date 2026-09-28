# deploy-ops-mcp-server

**只做一件事**：把某个 Deployment 滚到指定镜像，并如实回报结果。

Streamable HTTP，默认 `127.0.0.1:8400`（git=8000/8100、applog=8200/8101、datasource=8300 之后的下一个）。

```bash
cp .env.example .env      # 按需改
uv run python -m deploy_ops_mcp_server
```

## 工具

| 工具 | 类型 | 做什么 |
|---|---|---|
| `get_deployment_status(service)` | 只读 | 读**从 pod 上取回**的镜像与 pod 状态 |
| `rollout_deployment(service, image)` | **写** | 装镜像进节点 → `set image` → 等滚动 → **回读 pod 的镜像** |

**签名只收 `service`，不收 namespace/deployment/container** —— 那三个由本 server 自己的
`DEPLOY_OPS_TARGETS` 持有。理由：它们是**集群侧的事实**（与 agentflow 的
`action_executor._check_ns` 同一判据：namespace 该由持有集群凭证的那一方裁决），
而且让调用方传就多一个"传错"的面 —— **滚成功但滚错地方比失败更难查**。

```bash
DEPLOY_OPS_TARGETS='{"order-service": {"namespace": "order", "deployment": "order-service", "container": "order-service"}}'
```

缺条 ⇒ 该服务的调用 **fail-closed 报错**，不猜一个默认命名空间。

## ⚠️ 拓扑约束（承重）

**本 server 必须与 docker/podman daemon 跑在同一台机器上。**

镜像由 CI 构建在**宿主**的 docker/podman store 里，而 `rollout_deployment` 的第一步
`minikube image load` 要读那个 store。**不经 registry**（这是刻意的：testbed 的
Deployment 写死 `imagePullPolicy: Never`，本地 load 是最短路径）。

违反这条约束时本 server 照常启动、照常收请求，**症状只在某次真的滚到一半时出现**，
而报错是 `ImagePullBackOff` 或 `minikube image load 失败` —— 离"这台机器上没有
docker daemon"很远。

所以：**启动时自检**（探 docker daemon + `minikube status`），探不到就**拒绝启动**
（`server.py` 的 lifespan）。与 agentflow 的 `make doctor` 同一个理由 ——
把问题摆在装环境的时候。

## 为什么单独一台 server，不并进 `aiops-datasource-mcp-server`

1. 那台 server 的写面是**被文档化的单一写面**（`backends/k8s.py` 写着"不提供
   apply/delete/exec 等写操作"，`server.py` 写着"唯一的写工具是 `returnApmTicketStatus`"）。
2. **绑定粒度是 server 级**（agentflow `agent_configs.mcp_server_ids`）——
   那台已被 **9 个诊断 agent** 绑着，往它上面加写工具等于把这把枪发给所有 agent。
3. 身份不同：那台是"**读**数据源"，这台是"**写**集群"。

## 失败怎么报

| 情况 | 处理 |
|---|---|
| service 不在目标表 / 参数非法 / CLI 不存在 | **抛错**（部署错误，重试无用） |
| image load 失败 / set image 失败 / 滚动超时 / 回读不符 | **`{success: false, stage: ...}`**（这次的结果，调用方要如实上报） |

`stage` 取值：`image_load` / `set_image` / `rollout_status` / `read_back`。

**不自动回滚** —— 自动回滚会掩盖失败（run 报 failed 而线上已被悄悄换回去）。
失败文案里**带回滚命令**，给人信息、不替人做决定。

## 两个承重的判据

1. **`image load` 失败就停，不做 `set image`** —— 否则集群拉不到那个 tag，
   `rollout status` 白等一整轮超时，而报的是 `ImagePullBackOff`。
2. **收尾必须回读 pod 的镜像** —— `rollout status` 成功 ≠ 跑着的是我要的镜像
   （`replicas: 0` 时 `set image` 会"成功"而没有任何 pod 起来）。

## 测试

```bash
uv run pytest servers/deploy-ops-mcp-server/tests -q
```
