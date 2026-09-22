"""白名单视图（v10）：把工具注册表硬收窄到 Skill 声明的可见集合。

视图不复制工具实例——all / get / names 只是过滤既有注册表；
系统级 load_skill 永远可见（不受白名单约束，Skill 可嵌套触发）。
目录型 Skill 的工具在激活时注册进底层注册表，若在白名单内自然可见。
"""

from __future__ import annotations

from ..tools.base import Tool

SYSTEM_TOOLS = frozenset({"load_skill"})


class WhitelistView:
    """只暴露白名单内工具的注册表视图。接口与 ToolRegistry 对齐。"""

    def __init__(self, base, allowed: set[str]) -> None:
        self._base = base
        self._allowed = set(allowed) | SYSTEM_TOOLS

    def register(self, tool: Tool) -> None:
        """注册穿透到底层（目录型工具激活时经此进入）；视图过滤不受影响。"""
        self._base.register(tool)

    def unregister(self, name: str) -> None:
        self._base.unregister(name)

    def get(self, name: str) -> Tool | None:
        if name not in self._allowed:
            return None
        return self._base.get(name)

    def all(self) -> tuple[Tool, ...]:
        return tuple(
            tool for name in sorted(self._allowed) if (tool := self._base.get(name)) is not None
        )

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in sorted(self._allowed) if self._base.get(name) is not None)
