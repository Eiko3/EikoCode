"""外部工具客户端（v6）：三阶段连接与调用。

一次连接：初始化握手（协商协议版本与客户端信息）→ 工具列表发现 → 工具调用。
连接在会话内复用，不每次调用重连；传输方式由配置推断（有 argv 走 stdio，
有地址走 HTTP）。服务端主动发起的请求本期不支持：回「方法不存在」并提示。
"""

from __future__ import annotations

from typing import Sequence

from ..config import McpServerConfig
from ..errors import ErrorKind, EikoCodeError
from . import protocol
from .transport import Transport
from .transport_http import CONNECT_TIMEOUT, HttpTransport
from .transport_stdio import StdioTransport


def make_transport(config: McpServerConfig) -> Transport:
    """按配置推断传输：有命令走本地子进程，有地址走远程 HTTP；两者必须二选一。"""
    if config.argv and config.url:
        raise EikoCodeError(
            ErrorKind.CONFIG, f"外部工具服务 {config.name} 不能同时指定 command 与 url"
        )
    if config.argv:
        return StdioTransport(list(config.argv), env=dict(config.env))
    if config.url:
        return HttpTransport(config.url)
    raise EikoCodeError(ErrorKind.CONFIG, f"外部工具服务 {config.name} 缺少 command 或 url")


class McpClient:
    """一个服务一条连接。"""

    def __init__(self, config: McpServerConfig):
        self._config = config
        self._transport = make_transport(config)
        self._connected = False

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def permission(self) -> str:
        return self._config.permission

    def connect(self) -> list[dict]:
        """握手 + 工具发现，返回远端工具声明列表。"""
        transport = self._transport
        try:
            transport.start()
            initialize = transport.request(
                protocol.make_request(
                    "initialize",
                    {
                        "protocolVersion": protocol.PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "EikoCode", "version": "0.1.0"},
                    },
                ),
                CONNECT_TIMEOUT,
            )
            protocol.raise_for_error(initialize)
            transport.notify(protocol.make_notification("notifications/initialized"))
            listed = transport.request(protocol.make_request("tools/list", {}), CONNECT_TIMEOUT)
            protocol.raise_for_error(listed)
        except EikoCodeError:
            self.close()
            raise
        self._connected = True
        result = listed.get("result") or {}
        tools = result.get("tools") or []
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict) -> dict:
        """调用远端工具，返回结果中的 result 对象。"""
        if not self._connected:
            raise EikoCodeError(ErrorKind.PROTOCOL, "外部工具服务未连接")
        response = self._transport.request(
            protocol.make_request("tools/call", {"name": name, "arguments": arguments}),
            self._config.timeout,
        )
        protocol.raise_for_error(response)
        return response.get("result") or {}

    def drain_notifications(self) -> list[dict]:
        """取走通知；其中服务端主动请求会被回绝（方法不存在）。"""
        pending: list[dict] = []
        for note in self._transport.drain_notifications():
            if protocol.is_server_request(note):
                self._transport.notify(
                    protocol.make_error_response(
                        note.get("id"),
                        protocol.METHOD_NOT_FOUND,
                        "EikoCode 不支持该方法",
                    )
                )
            pending.append(note)
        return pending

    def close(self) -> None:
        self._connected = False
        try:
            self._transport.close()
        except Exception:
            pass
