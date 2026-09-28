"""deploy-ops-mcp-server —— **只做一件事**：把某个 Deployment 滚到指定镜像，并如实回报结果。

## 为什么单独一台 server，不并进 aiops-datasource-mcp-server

三条都成立，前两条是硬的：

1. **那台 server 的写面是被文档化的单一写面**。它的 `backends/k8s.py` 写着
   「命令白名单（只 get/describe pod，**不提供 apply/delete/exec 等写操作**）」，
   `tools/ticket.py` 与 `server.py` 都写着「**唯一的写工具是 `returnApmTicketStatus`**」。
   往里加一个"滚 Deployment"的写工具，会让那三句话同时变成假话。

2. **绑定粒度是 server 级，不是工具级**（agentflow `docs/TODO.md` §16：
   `agent_configs.mcp_server_ids` 绑的是 server）。`aiops-datasource` 已经被
   **9 个诊断 agent** 绑着 —— 往它上面加写工具，等于把这把枪发给所有绑它的 agent
   （`code-locator` 也能滚线上）。

3. 身份也不同：那台是"**读**数据源"，这台是"**写**集群"。

## ⚠️ 拓扑约束（本 server 的承重前提）

**本 server 必须与 docker/podman daemon 跑在同一台机器上。**

镜像由 CI 构建在**宿主**的 docker/podman store 里，而本 server 的
`rollout_deployment` 第一步是 `minikube image load` —— 那要求它能读到那个 store。
把它搬进集群（很自然的下一步）会让这一步失败，**而报错离原因很远**。

所以 `server.py` 在启动时探一次（docker daemon + minikube 可达），探不到就**拒绝启动**。
"""

__version__ = "0.1.0"
