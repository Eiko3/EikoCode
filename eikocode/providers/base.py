"""供应商抽象层：与供应商无关的消息表示与流式生成接口。

本模块不 import 任何供应商 SDK——适配层各自把 SDK 的异常翻译成
`EikoCodeError`，这里只认统一之后的错误类型。

v2 起，流里除了文本块，还可能出现 `ToolCall`：模型在某轮里要求调用工具。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator, Sequence

from ..errors import EikoCodeError


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    # 助手消息里携带的工具调用（一轮可含多个，但本程序每轮串行执行其一）
    tool_calls: tuple[ToolCall, ...] = ()
    # 工具结果消息：对应哪一次工具调用的 id
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """模型要求执行一次工具调用。"""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Thinking:
    """模型思考内容（原生思考模式用）。

    空思考不应发送给上层——运行时只在其内容非空时才发出「模型思考」事件，
    避免空思考刷屏。
    """

    text: str = ""


@dataclass(frozen=True)
class UsageInfo:
    """一次响应的用量（缓存字段已归一，见 spec.md §3 能力 35）。

    供应商未提供某字段时为 0——「未命中」与「无数据」都显示为零，
    让缓存没生效这件事可以被看见，而不是被隐藏。
    """

    input_tokens: int = 0
    cache_hit: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class GenerateParams:
    model: str
    temperature: float
    max_tokens: int
    # 稳定前缀（系统消息文本，v4）。由拼装器生成，会话内逐字节不变；
    # 空串表示不带系统消息（保持 v1–v3 的裸请求行为）。
    system: str = ""


class Provider(ABC):
    """一家供应商的适配器。"""

    name = "base"

    # 供应商是否接受采样参数。Anthropic 自 Opus 4.7 / Sonnet 5 起不再接受，
    # 传非默认值会直接 400，所以那里只能不带这个参数。
    supports_temperature = False

    # 供应商是否支持工具调用（原生 tool_use / function calling）。
    # 不支持的模型在启动阶段即被拒绝进入带工具的模式，不静默降级。
    supports_tools = True

    @abstractmethod
    def stream(
        self,
        messages: Sequence[Message],
        params: GenerateParams,
        tools: Sequence = (),
    ) -> Iterator[str | ToolCall | Thinking | UsageInfo]:
        """产出文本块、工具调用请求、思考内容或用量信息。

        每个文本块都是可直接拼接的增量文本；工具调用请求以 `ToolCall` 出现；
        思考内容以 `Thinking` 出现（可空，上层只在非空时渲染）；
        `UsageInfo` 在流中至多出现一次（含缓存命中字段），上层记录后供用量视图使用。
        原生协议下工具调用通常在流的末尾才完整，适配层负责在内部累积后产出。
        """
        raise NotImplementedError


MAX_RETRIES = 2
BACKOFF_SECONDS = (1.0, 2.0)


def _run_stream(factory: Callable[[], Iterator], emit: Callable[[object], None]) -> None:
    """带退避重试地跑一次流式生成（文本或工具调用共用）。

    只有在一个块都还没产出时才重试。一旦有任何内容（文本或工具调用）吐给了用户，
    再重试就会在屏幕上接出两段拼在一起的半截回复，那就得不偿失了。
    """
    last: EikoCodeError | None = None
    for attempt in range(MAX_RETRIES + 1):
        produced = False
        try:
            for chunk in factory():
                produced = True
                emit(chunk)
            return
        except EikoCodeError as exc:
            last = exc
            if produced or not exc.retryable or attempt >= MAX_RETRIES:
                raise
            time.sleep(BACKOFF_SECONDS[attempt])
    if last is not None:
        raise last


def run_stream_with_retry(
    factory: Callable[[], Iterator[str]], emit: Callable[[str], None]
) -> None:
    """纯文本路径（向后兼容）的带重试流式执行。"""
    _run_stream(factory, emit)  # type: ignore[arg-type]


def run_with_tools(
    factory: Callable[[], Iterator[str | ToolCall]],
    emit: Callable[[str | ToolCall], None],
) -> None:
    """带工具调用的流式路径：文本块与工具调用请求共用同一重试逻辑。"""
    _run_stream(factory, emit)  # type: ignore[arg-type]
