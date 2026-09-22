"""OpenAI 兼容协议适配。

覆盖 OpenAI 官方、DeepSeek、通义、以及本地的 Ollama / vLLM——
凡是走 /chat/completions 的都归这一家，base_url 可指过去。
这一家仍然接受采样参数，也支持 function calling（工具调用）。

工具调用：模型在流的 `delta.tool_calls` 里分片吐出 id / name / arguments JSON，
这里按 index 在内部累积，待整轮流结束再产出完整的 `ToolCall`。
"""

from __future__ import annotations

import json
from typing import Iterator, Sequence

import httpx
import openai

from ..errors import ErrorKind, EikoCodeError
from .base import GenerateParams, Message, Provider, ToolCall, UsageInfo


CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 120.0


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)


def _tool_schemas(tools: Sequence) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _to_openai_messages(messages: Sequence[Message]) -> list[dict]:
    payload: list[dict] = []
    for m in messages:
        if m.role.value == "user":
            if m.tool_call_id is not None:
                payload.append(
                    {
                        "role": "tool",
                        "content": m.content,
                        "tool_call_id": m.tool_call_id,
                    }
                )
            else:
                payload.append({"role": "user", "content": m.content})
        else:  # assistant
            if m.tool_calls:
                payload.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
            else:
                payload.append({"role": "assistant", "content": m.content})
    return payload


class OpenAICompatProvider(Provider):
    name = "openai_compat"
    supports_temperature = True

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        client: openai.OpenAI | None = None,
    ) -> None:
        self._client = client or openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=_timeout(),
            max_retries=0,
        )

    def stream(
        self,
        messages: Sequence[Message],
        params: GenerateParams,
        tools: Sequence = (),
    ) -> Iterator[str | ToolCall | UsageInfo]:
        payload = _to_openai_messages(messages)
        if params.system:
            # 稳定前缀以首条 system 消息进入请求。这一家是隐式前缀缓存：
            # 无需任何标记，system + tools + 历史顺序逐字节一致即自动命中。
            payload = [{"role": "system", "content": params.system}] + payload
        kwargs: dict = {
            "model": params.model,
            "messages": payload,
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
            "stream": True,
            # 流式下 usage 挂在最后一个 chunk；不请求就永远拿不到缓存字段。
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = _tool_schemas(tools)

        try:
            stream = self._client.chat.completions.create(**kwargs)
            accumulated: dict[int, dict] = {}
            for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    # DeepSeek 字段：prompt_cache_hit_tokens / prompt_cache_miss_tokens。
                    # 无对应字段时按 0 归一（「没生效」要可见，不是隐藏）。
                    yield UsageInfo(
                        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        cache_hit=getattr(usage, "prompt_cache_hit_tokens", 0) or 0,
                        cache_write=0,
                    )
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    yield text
                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", 0) or 0
                    slot = accumulated.setdefault(idx, {"id": "", "name": "", "args": ""})
                    func = getattr(tc, "function", None)
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    if func is not None and getattr(func, "name", None):
                        slot["name"] = func.name
                    if func is not None and getattr(func, "arguments", None):
                        slot["args"] += func.arguments
            for slot in accumulated.values():
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield ToolCall(id=slot["id"], name=slot["name"], arguments=args)
        except openai.AuthenticationError as exc:
            raise EikoCodeError(ErrorKind.AUTH, type(exc).__name__) from exc
        except openai.RateLimitError as exc:
            raise EikoCodeError(ErrorKind.RATE_LIMIT, type(exc).__name__) from exc
        except openai.APITimeoutError as exc:
            raise EikoCodeError(ErrorKind.TIMEOUT, type(exc).__name__) from exc
        except openai.APIConnectionError as exc:
            raise EikoCodeError(ErrorKind.NETWORK, type(exc).__name__) from exc
        except openai.APIStatusError as exc:
            if getattr(exc, "status_code", None) == 402:
                # 余额不足是用户可直接行动的状态，不该伪装成「响应格式异常」。
                raise EikoCodeError(ErrorKind.BILLING, "402") from exc
            kind = ErrorKind.NETWORK if exc.status_code >= 500 else ErrorKind.PROTOCOL
            raise EikoCodeError(kind, type(exc).__name__) from exc
        except openai.APIError as exc:
            raise EikoCodeError(ErrorKind.UNKNOWN, type(exc).__name__) from exc
        except openai.OpenAIError as exc:
            raise EikoCodeError(ErrorKind.UNKNOWN, type(exc).__name__) from exc
