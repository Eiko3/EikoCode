"""命令注册中心（v9）：集中登记全部斜杠命令。

每条命令一条 `CommandSpec`；注册时检测名称与别名冲突，冲突立即报错——
不允许同名命令悄悄互相覆盖（spec.md §3 能力 66）。别名是完整命令的
等价入口，解析与补全都一视同仁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..errors import ErrorKind, EikoCodeError


class CommandKind:
    """执行类型：决定 REPL 对命令结果的处理路径。"""

    LOCAL = "local"      # 纯本地处理（/help /usage /sessions …）
    UI = "ui"            # 操作界面或会话状态（/clear /plan /model /resume …）
    PROMPT = "prompt"    # 预设提示词送对话流由模型处理（/review /explain）


@dataclass(frozen=True)
class CommandSpec:
    """一条斜杠命令的完整描述。"""

    name: str
    description: str
    usage: str
    kind: str
    handler: Callable  # handler(ctx: CommandContext, arg: str) -> CommandResult
    param_hint: str = ""
    aliases: tuple[str, ...] = field(default=())
    hidden: bool = False  # 隐藏命令：不参与补全与 /help，仍可执行


class CommandRegistry:
    """名称与别名 → 命令的集中注册表。"""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._primaries: list[CommandSpec] = []

    def register(self, spec: CommandSpec) -> None:
        names = [spec.name, *spec.aliases]
        for name in names:
            if name in self._commands:
                raise EikoCodeError(
                    ErrorKind.TOOL_INVALID_ARG, f"命令冲突：{name} 已被注册"
                )
        for name in names:
            self._commands[name] = spec
        self._primaries.append(spec)

    def get(self, name: str) -> CommandSpec | None:
        """按名称或别名查找（大小写不敏感在解析层完成）。"""
        return self._commands.get(name)

    def unregister(self, name: str) -> None:
        """注销一条命令（v10 短命令随 Skill 清空/重建而移除）。

        主名注销时连同别名与其 primaries 记录一起移除；别名调用则静默。
        """
        spec = self._commands.pop(name, None)
        if spec is None:
            return
        if spec.name == name:
            self._primaries = [s for s in self._primaries if s.name != spec.name]
            for alias in spec.aliases:
                if self._commands.get(alias) is spec:
                    self._commands.pop(alias, None)

    def visible(self) -> list[CommandSpec]:
        """非隐藏命令（按主名去重），供 /help 使用。"""
        return [spec for spec in self._primaries if not spec.hidden]

    def completion_candidates(self) -> list[str]:
        """Tab 补全候选：可见命令的名称与别名。"""
        out: list[str] = []
        for spec in self._primaries:
            if spec.hidden:
                continue
            out.append(spec.name)
            out.extend(spec.aliases)
        return sorted(out)

    def __len__(self) -> int:
        return len(self._primaries)
