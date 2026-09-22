"""Anthropic 原生协议适配。

注意：Anthropic 自 Opus 4.7 / Sonnet 5 起不再接受 temperature、top_p、top_k，
传非默认值会直接返回 400，SDK v1.0.0 也把这些参数从方法签名里移除了。
所以这里只带 model / messages / max_tokens，采样参数一概不传。

工具调用：模型在流里给出 tool_use 内容块，SDK 以 `content_block_*` 事件序列
吐出；这里在内部累积每个块的 id / name / 入参 JSON，待块结束再产出 `ToolCall`。
"""

from __future__ import annotations

import json
from typing import Iterator, Sequence

import anthropic
import httpx

from ..errors import ErrorKind, EikoCodeError
from .base import GenerateParams, Message, Provider, ToolCall, UsageInfo

# 稳定前缀整体打一个缓存断点（提议默认，见 checklist.md 组 28）：
# Anthropic 是显式断点制，不打标记就一次缓存都不会有。
CACHE_CONTROL = {"type": "ephemeral"}


CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 120.0


def _timeout() -> httpx.Timeout:
    """连接上限 30 秒，之后每次读取上限 120 秒（即「多久没新数据算超时」）。"""
    return httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)


def _tool_schemas(tools: Sequence) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def _as_content_blocks(content) -> list[dict]:
    """把 str 或块列表统一为块列表（空文本产出空列表）。"""
    if isinstance(content, list):
        return content
    if content:
        return [{"type": "text", "text": content}]
    return []


def _merge_consecutive_roles(payload: list[dict]) -> list[dict]:
    """合并相邻的同角色消息为一条（content 块串联）。

    Anthropic 要求 user / assistant 严格交替。运行时注入与环境追加是
    user 角色的补充消息，紧跟在工具结果（也是 user 角色）之后——不合并
    会被 API 直接 400 拒绝。块顺序保持原样：tool_result 块在前、文本块在后。
    """
    merged: list[dict] = []
    for item in payload:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"] = (
                _as_content_blocks(merged[-1]["content"]) + _as_content_blocks(item["content"])
            )
        else:
            merged.append(item)
    return merged


def _to_anthropic_messages(messages: Sequence[Message]) -> list[dict]:
    payload: list[dict] = []
    for m in messages:
        if m.role.value == "user":
            if m.tool_call_id is not None:
                payload.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                payload.append({"role": "user", "content": m.content})
        else:  # assistant
            if m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                payload.append({"role": "assistant", "content": blocks})
            else:
                payload.append({"role": "assistant", "content": m.content})
    return _merge_consecutive_roles(payload)


class AnthropicProvider(Provider):
    name = "anthropic"
    supports_temperature = False

    def __init__(self, api_key: str, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=_timeout(), max_retries=0
        )

    def stream(
        self,
        messages: Sequence[Message],
        params: GenerateParams,
        tools: Sequence = (),
    ) -> Iterator[str | ToolCall]:
        payload = _to_anthropic_messages(messages)
        kwargs: dict = {
            "model": params.model,
            "messages": payload,
            "max_tokens": params.max_tokens,
        }
        if params.system:
            # system 以块列表传递，末块带缓存断点：工具声明与 system 一起
            # 构成稳定前缀，断点之后的会话历史变化不破坏前缀缓存。
            kwargs["system"] = [
                {"type": "text", "text": params.system, "cache_control": CACHE_CONTROL}
            ]
        if tools:
            kwargs["tools"] = _tool_schemas(tools)

        try:
            with self._client.messages.stream(**kwargs) as stream:
                current: dict | None = None
                for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "message_start":
                        usage = getattr(getattr(event, "message", None), "usage", None)
                        if usage is not None:
                            yield UsageInfo(
                                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                                cache_hit=getattr(usage, "cache_read_input_tokens", 0) or 0,
                                cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                            )
                    elif etype == "content_block_delta":
                        delta = event.delta
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield text
                        elif dtype == "input_json_delta":
                            if current is not None:
                                current["args"] += getattr(delta, "partial_json", "") or ""
                    elif etype == "content_block_start":
                        block = event.content_block
                        if getattr(block, "type", None) == "tool_use":
                            current = {
                                "id": block.id,
                                "name": block.name,
                                "args": "",
                            }
                    elif etype == "content_block_stop":
                        if current is not None:
                            try:
                                args = json.loads(current["args"] or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            yield ToolCall(id=current["id"], name=current["name"], arguments=args)
                            current = None
        except anthropic.AuthenticationError as exc:
            raise EikoCodeError(ErrorKind.AUTH, type(exc).__name__) from exc
        except anthropic.PermissionDeniedError as exc:
            raise EikoCodeError(ErrorKind.AUTH, type(exc).__name__) from exc
        except anthropic.RateLimitError as exc:
            raise EikoCodeError(ErrorKind.RATE_LIMIT, type(exc).__name__) from exc
        except anthropic.APITimeoutError as exc:
            raise EikoCodeError(ErrorKind.TIMEOUT, type(exc).__name__) from exc
        except anthropic.APIConnectionError as exc:
            raise EikoCodeError(ErrorKind.NETWORK, type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            kind = ErrorKind.NETWORK if exc.status_code >= 500 else ErrorKind.PROTOCOL
            raise EikoCodeError(kind, type(exc).__name__) from exc
        except anthropic.APIError as exc:
            raise EikoCodeError(ErrorKind.UNKNOWN, type(exc).__name__) from exc
        except anthropic.AnthropicError as exc:
            raise EikoCodeError(ErrorKind.UNKNOWN, type(exc).__name__) from exc
