"""代理运行时包：事件流 + ReAct 循环 + 批处理 + plan-only + 可取消。

本包不触碰呈现——只产出 AgentEvent，由 CLI / 未来的 TUI 订阅渲染。
"""

from __future__ import annotations

from .events import AgentEvent, EventKind
from .loop import AgentRuntime, CancelToken

__all__ = [
    "AgentEvent",
    "EventKind",
    "AgentRuntime",
    "CancelToken",
]
