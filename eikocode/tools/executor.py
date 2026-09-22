"""工具层：统一执行入口。

套超时约束，并把执行期的各类异常归一为有限的错误类别。超时触发时真正终止
底层进程——调用工具自身的 `kill()`（Shell 工具会杀掉整个进程树），不残留孤儿。
"""

from __future__ import annotations

import threading

from ..errors import ErrorKind, EikoCodeError
from .base import Tool

# 工具默认执行超时（秒）。清单 11 规定为 120 秒（提议默认）。
DEFAULT_TIMEOUT = 120


def execute_tool(tool: Tool, arguments: dict, timeout: int = DEFAULT_TIMEOUT) -> str:
    """在线程里执行工具，受 `timeout` 秒约束。

    - 工具主动抛出的 `EikoCodeError` 原样上浮（已归一化）。
    - 其它未预期异常归一为 `TOOL_ERROR`，绝不把原始堆栈甩给用户。
    - 超时：先调用 `tool.kill()` 终止底层进程，再判定为 `TIMEOUT`。
    """
    box: dict = {}
    err: dict = {}

    def run() -> None:
        try:
            box["result"] = tool.execute(arguments)
        except EikoCodeError as exc:
            err["mew"] = exc
        except Exception as exc:  # 兜底归一
            err["mew"] = EikoCodeError(
                ErrorKind.TOOL_ERROR, f"工具执行出错：{type(exc).__name__}"
            )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        try:
            tool.kill()
        except Exception:
            pass
        worker.join(2.0)
        raise EikoCodeError(ErrorKind.TIMEOUT, f"工具执行超过 {timeout} 秒，已被终止")

    if "mew" in err:
        raise err["mew"]
    return box["result"]
