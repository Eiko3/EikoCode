"""外部工具接入（v6，MCP 客户端）。

以 JSON-RPC 2.0 与外部工具服务通信：本地子进程 stdio 或远程 Streamable HTTP。
一次连接三阶段——初始化握手 → 工具列表发现 → 工具调用；连接会话内复用，
工具经适配层包装为本地统一工具接口后注册进工具中心，代理运行时无感调用。

明示边界（spec.md §6「v6 本章明确不做」）：只做客户端；只处理文本内容块；
服务端主动发起的请求回「方法不存在」；工具列表不做热更新。
"""

from .adapter import RemoteTool
from .client import McpClient

__all__ = ["McpClient", "RemoteTool"]
