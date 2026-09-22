"""工具层：集中注册表。

按名称登记全部工具，供供应商层翻译 schema、供 CLI 在收到工具调用时按名查找。
重复注册同一名称立即报错——注册表是唯一的，不允许同名工具悄悄互相覆盖。
"""

from __future__ import annotations

from ..errors import ErrorKind, EikoCodeError
from .base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise EikoCodeError(
                ErrorKind.TOOL_INVALID_ARG, f"工具名重复注册：{tool.name}"
            )
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """移除一个工具（v6 重建外部工具连接时用）。不存在则静默。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)
