"""工具层入口：集中注册全部工具，供 CLI 按名查找与翻译 schema。"""

from __future__ import annotations

from .base import PermissionLevel, Tool, ToolSpec
from .edit_file import EditFileTool
from .executor import DEFAULT_TIMEOUT, execute_tool
from .read_file import ReadFileTool
from .registry import ToolRegistry
from .search import GlobTool, GrepTool
from .shell import ShellTool
from .write_file import WriteFileTool

__all__ = [
    "PermissionLevel",
    "Tool",
    "ToolSpec",
    "ToolRegistry",
    "DEFAULT_TIMEOUT",
    "execute_tool",
    "get_registry",
]


def get_registry() -> ToolRegistry:
    """构造一个装好全部内置工具的注册表。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(ShellTool())
    return registry
