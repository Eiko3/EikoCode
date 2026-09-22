"""事件流通道（v3）。

代理运行时以迭代器形式逐事件吐出循环过程；上层（CLI / TUI）订阅并逐类渲染。
事件类型至少覆盖以下七类；`AgentEvent` 是上层消费的统一载体。

循环逻辑不内联在 CLI 中——CLI 只消费这些事件，不直接驱动工具循环。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventKind(str, Enum):
    """事件类型。值即中文标签，供上层渲染与测试识别。"""

    USER_MESSAGE = "用户消息"
    THINKING = "模型思考"
    TEXT_DELTA = "文本增量"
    TOOL_CALL_START = "工具调用开始"
    TOOL_RESULT = "工具结果"
    FINAL_REPLY = "最终回复"
    ERROR = "错误"


@dataclass(frozen=True)
class AgentEvent:
    """一条事件。"""

    kind: EventKind
    text: str = ""
    tool_name: str = ""
    is_error: bool = False

    @classmethod
    def user_message(cls, text: str) -> "AgentEvent":
        return cls(EventKind.USER_MESSAGE, text=text)

    @classmethod
    def thinking(cls, text: str) -> "AgentEvent":
        return cls(EventKind.THINKING, text=text)

    @classmethod
    def text_delta(cls, text: str) -> "AgentEvent":
        return cls(EventKind.TEXT_DELTA, text=text)

    @classmethod
    def tool_call_start(cls, name: str) -> "AgentEvent":
        return cls(EventKind.TOOL_CALL_START, tool_name=name)

    @classmethod
    def tool_result(cls, name: str, text: str) -> "AgentEvent":
        return cls(EventKind.TOOL_RESULT, tool_name=name, text=text)

    @classmethod
    def final_reply(cls, text: str) -> "AgentEvent":
        return cls(EventKind.FINAL_REPLY, text=text)

    @classmethod
    def error(cls, text: str) -> "AgentEvent":
        return cls(EventKind.ERROR, text=text, is_error=True)
