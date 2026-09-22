"""摘要生成器（v7）：兜底层的 LLM 调用。

提示首尾各强调一次「禁止调用任何工具」；要求模型先输出分析草稿再输出
正式摘要（<draft> / <summary> 标签），解析只取正式部分，草稿用完即弃。
摘要请求与响应都不进入会话历史（spec.md §3 能力 55）。

「用户原话」部分不由模型生成——由代码在压缩时逐字拼接（见 compaction），
从根上保证原话不被改写。
"""

from __future__ import annotations

from typing import Sequence

from ..errors import ErrorKind, EikoCodeError
from ..providers.base import GenerateParams, Message, Provider, Role

# 固定九部分（「用户原话」由代码拼接，此处为其余八部分 + 提示词里的九节标题清单）
SUMMARY_SECTIONS = (
    "主要请求",
    "关键概念",
    "文件与代码",
    "错误与修复",
    "解决过程",
    "用户原话",
    "待办",
    "当前工作",
    "下一步",
)

_NO_TOOL_LINE = "禁止调用任何工具——本次请求只生成文本摘要，不执行任何操作。"

_PROMPT_HEAD = (
    "【任务】把下面的对话历史压缩为结构化摘要，供后续对话作为早期记忆使用。\n"
    f"【硬性要求】{_NO_TOOL_LINE}\n"
    "【输出格式】严格按以下顺序输出，不要省略标签：\n"
    "<draft>\n（先在这里写你的分析草稿：快速梳理对话脉络，草稿用完即弃）\n</draft>\n"
    "<summary>\n（正式摘要，必须包含以下九个小节，每个小节用「## 标题」开头，"
    "按对话实际内容填写，没有内容的写「（无）」）：\n"
    + "\n".join(f"## {name}" for name in SUMMARY_SECTIONS)
    + "\n其中「用户原话」小节会由系统自动附加历史中的用户消息原文，你只需留出小节标题。\n</summary>"
)

_PROMPT_TAIL = f"【再次强调】{_NO_TOOL_LINE}"


def _serialize(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        if m.role is Role.USER and m.tool_call_id is not None:
            lines.append(f"[工具结果·{m.tool_call_id}] {m.content}")
        elif m.role is Role.USER:
            lines.append(f"[用户] {m.content}")
        elif m.tool_calls:
            names = ", ".join(tc.name for tc in m.tool_calls)
            body = f"[助手·调用工具：{names}] {m.content}".rstrip()
            lines.append(body)
        else:
            lines.append(f"[助手] {m.content}")
    return "\n".join(lines)


def build_prompt(messages: Sequence[Message]) -> str:
    return f"{_PROMPT_HEAD}\n\n【对话历史】\n{_serialize(messages)}\n\n{_PROMPT_TAIL}"


def parse_summary(text: str) -> str:
    """取 <summary> 标签内的正式摘要；标签缺失时容错取全文。"""
    if "<summary>" in text and "</summary>" in text:
        return text.split("<summary>", 1)[1].split("</summary>", 1)[0].strip()
    return text.strip()


def summarize(provider: Provider, model: str, messages: Sequence[Message]) -> str:
    """生成正式摘要。失败抛 EikoCodeError，由熔断器记录。"""
    prompt = build_prompt(messages)
    params = GenerateParams(model=model, temperature=0.2, max_tokens=8192)
    system = "你是 EikoCode 的会话摘要助手。只输出文本。" + _NO_TOOL_LINE
    chunks: list[str] = []
    for item in provider.stream(
        [Message(role=Role.USER, content=prompt)],
        GenerateParams(
            model=model,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
            system=system,
        ),
    ):
        if isinstance(item, str):
            chunks.append(item)
    text = "".join(chunks).strip()
    if not text:
        raise EikoCodeError(ErrorKind.PROTOCOL, "摘要生成返回为空")
    summary = parse_summary(text)
    if not summary:
        raise EikoCodeError(ErrorKind.PROTOCOL, "摘要解析结果为空")
    return summary
