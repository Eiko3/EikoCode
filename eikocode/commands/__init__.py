"""命令框架（v9）：注册中心、解析器、界面控制接口、内置命令、Tab 补全。

REPL 回车入口经解析器分流：命令按执行类型分发，非命令原样送对话流
（spec.md §3 能力 66–73）。
"""

from .builtin import build_builtin_registry
from .completer import complete_command, install_readline_completion
from .context import CommandContext
from .parser import ParsedInput, parse
from .registry import CommandKind, CommandRegistry, CommandSpec

__all__ = [
    "CommandContext",
    "CommandKind",
    "CommandRegistry",
    "CommandSpec",
    "ParsedInput",
    "build_builtin_registry",
    "complete_command",
    "install_readline_completion",
    "parse",
]
