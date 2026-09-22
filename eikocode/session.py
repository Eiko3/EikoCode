"""会话层：多轮累积与中断回滚。

唯一的硬规则：**助手消息只在一次回复完整结束后才追加**。
中断或报错就整条丢掉，不做任何补偿——上下文只由用户显式改变。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .providers.base import Message, Role, ToolCall


@dataclass
class Session:
    _messages: list[Message] = field(default_factory=list)

    def add_user(self, content: str) -> None:
        self._messages.append(Message(role=Role.USER, content=content))

    def complete_assistant(self, content: str) -> None:
        """一次回复完整结束后调用。空回复不入库。"""
        if not content.strip():
            return
        self._messages.append(Message(role=Role.ASSISTANT, content=content))

    def add_assistant_with_tools(
        self, content: str, tool_calls: tuple[ToolCall, ...]
    ) -> None:
        """一次回复里带工具调用时调用。文本（若有）与工具调用一起入库。"""
        self._messages.append(
            Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(tool_calls))
        )

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """工具执行结果作为用户侧消息回写，供供应商据此继续生成。"""
        self._messages.append(
            Message(role=Role.USER, content=content, tool_call_id=tool_call_id)
        )

    def truncate(self, length: int) -> None:
        """回滚到指定长度。工具执行中被中断或失败时，整轮（含本轮回的所有消息）不回写。"""
        if length < 0:
            length = 0
        if length >= len(self._messages):
            return
        del self._messages[length:]

    def drop_last(self) -> None:
        """撤销最近一条消息，用于请求没发出去或彻底失败时回滚。"""
        if self._messages:
            self._messages.pop()

    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def rewrite(self, messages) -> None:
        """整体替换消息列表（v7 压缩专用：摘要替换旧轮次）。

        与回滚不同：这是用户可见的压缩动作，替换前后都会在终端说明。
        """
        self._messages = list(messages)

    def clear(self) -> None:
        self._messages.clear()

    @property
    def turns(self) -> int:
        """轮次 = 用户消息条数。"""
        return sum(1 for m in self._messages if m.role is Role.USER)

    @property
    def message_count(self) -> int:
        return len(self._messages)
