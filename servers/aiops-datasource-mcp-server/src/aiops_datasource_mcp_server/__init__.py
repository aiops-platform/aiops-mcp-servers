"""aiops-datasource-mcp-server — 领域型只读数据源 MCP Server（ES / Prometheus / K8s）。

与 applog-mcp-server（声明式 HTTP 透传）不同，本 server 暴露的是**语义工具**：
调用方传 `metric=cpu_percent` 这样的领域语义，而非裸 PromQL 表达式——
PromQL 映射住在 server 侧，agent 无法写错查询（见 design-v5.5 §5）。
"""

__version__ = "0.1.0"
