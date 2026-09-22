"""外部工具接入的连接管理（v6）。

启动时按配置连接全部服务并把工具注册进工具中心；连接失败明确指出是
哪个服务与原因，该服务的工具不进入工具集，其余能力照常（不静默降级）。
连接在会话内复用；工具集会话内固定——变更需显式重建，并告知缓存前缀
会失效一次（spec.md §3 能力 48）。
"""

from __future__ import annotations

from typing import Callable, Sequence

from ..config import Config
from ..errors import EikoCodeError
from ..tools.base import Tool
from ..tools.registry import ToolRegistry
from .adapter import RemoteTool
from .client import McpClient

RELOAD_NOTICE = "工具集已变更，缓存前缀本次失效"


class McpManager:
    """连接、注册、重建与回收外部工具服务。"""

    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        notify: Callable[[str], None],
    ) -> None:
        self._config = config
        self._registry = registry
        self._notify = notify
        self._clients: list[McpClient] = []
        self._remote_names: list[str] = []

    @property
    def tool_count(self) -> int:
        return len(self._remote_names)

    @property
    def server_count(self) -> int:
        return len(self._clients)

    def connect_all(self) -> None:
        """连接全部配置服务并注册工具；单个失败不影响其余。"""
        # 配置错误先明确报出（该服务不进工具集，其余照常）
        for message in getattr(self._config, "mcp_errors", ()) or ():
            self._notify(f"外部工具服务配置错误：{message}")
        for server_config in self._config.mcp_servers:
            client = McpClient(server_config)
            try:
                specs = client.connect()
            except EikoCodeError as exc:
                self._notify(
                    f"外部工具服务 {server_config.name} 连接失败：{exc.user_message}"
                    f"；该服务的工具不可用"
                )
                client.close()
                continue
            registered = 0
            for spec in specs:
                tool = RemoteTool(server_config.name, spec, client, server_config.permission)
                if tool.name in self._registry.names():
                    self._notify(f"外部工具 {tool.name} 与既有工具重名，已跳过")
                    continue
                self._registry.register(tool)
                self._remote_names.append(tool.name)
                registered += 1
            self._clients.append(client)
            self._notify(
                f"外部工具服务 {server_config.name} 已连接：注册 {registered} 个工具"
            )

    def reload(self) -> None:
        """关闭并重连；工具集变化后提示缓存前缀失效一次。"""
        self.close()
        self.connect_all()
        self._notify(RELOAD_NOTICE)

    def status_lines(self) -> list[str]:
        if not self._clients:
            return ["未连接任何外部工具服务"]
        lines = [
            f"已连接 {self.server_count} 个服务，共 {self.tool_count} 个外部工具"
        ]
        for client in self._clients:
            lines.append(f"  {client.name}（权限：{client.permission}）")
        return lines

    def drain_notifications(self) -> None:
        """处理通知；服务端主动请求会被回绝。"""
        for client in self._clients:
            try:
                client.drain_notifications()
            except Exception:
                continue

    def close(self) -> None:
        for client in self._clients:
            try:
                client.close()
            except Exception:
                pass
        self._clients = []
        # 远端工具从注册表移除，便于重建时重新注册
        for name in self._remote_names:
            self._registry.unregister(name)
        self._remote_names = []
