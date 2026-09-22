"""压缩流水（v7）：预防层与兜底集成的核心实现。

预防层在工具结果回写会话**之前**就地生效（单条超限存盘、合计超限挑大
存盘）；兜底层在每次请求前检查累计用量，达到阈值时生成结构化摘要替换
较早轮次，最近数轮保持原文，并附加边界消息防臆造。摘要连续失败达到
上限即熔断，停止自动触发；手动触发绕过熔断但仍记录失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..errors import EikoCodeError
from ..providers.base import Message, Role
from . import summarizer
from .snapshots import save_snapshot

# 累计用量达到该比例时自动兜底压缩（提议默认 90%，见 checklist 组 53）
COMPACT_RATIO = 0.90

# 兜底保留的最近轮数（提议默认 3，见 checklist 组 53）
KEEP_RECENT_TURNS = 3

# 摘要连续失败上限（提议默认 3，见 checklist 组 54）
BREAKER_MAX_FAILURES = 3

BOUNDARY_MESSAGE = (
    "以上是较早对话的压缩摘要。文件与代码的细节可能不完整——"
    "需要引用具体内容时请重新用工具读取，不要根据摘要臆造。"
)

# 预防层各阈值的缺省值（配置可覆盖；提议默认见 checklist 组 51）
DEFAULT_TOOL_RESULT_CHARS = 20_000
DEFAULT_MESSAGE_CHARS = 40_000
DEFAULT_PREVIEW_CHARS = 1_500


@dataclass
class CompactBreaker:
    """摘要连续失败熔断：达到上限停止自动触发；一次成功即清零。"""

    max_failures: int = BREAKER_MAX_FAILURES
    failures: int = 0
    open: bool = False

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.max_failures:
            self.open = True

    def record_success(self) -> None:
        self.failures = 0
        self.open = False


@dataclass
class _CompactConfig:
    """预防层阈值集合（从全局配置提取，getattr 兜底便于测试）。"""

    tool_result_chars: int = DEFAULT_TOOL_RESULT_CHARS
    message_chars: int = DEFAULT_MESSAGE_CHARS
    preview_chars: int = DEFAULT_PREVIEW_CHARS


def _config_from(config) -> _CompactConfig:
    return _CompactConfig(
        tool_result_chars=int(
            getattr(config, "compact_tool_result_chars", DEFAULT_TOOL_RESULT_CHARS)
        ),
        message_chars=int(
            getattr(config, "compact_message_chars", DEFAULT_MESSAGE_CHARS)
        ),
        preview_chars=int(
            getattr(config, "compact_preview_chars", DEFAULT_PREVIEW_CHARS)
        ),
    )


def _shrink(text: str, tool_name: str, cfg: _CompactConfig, notify, save) -> tuple[str, bool]:
    """存盘并生成预览。写盘失败时返回「已阻止发送」的错误结果（回退行为）。"""
    try:
        path = save(text, tool_name)
    except EikoCodeError as exc:
        notify(f"快照写入失败：{exc.user_message}；该工具结果已阻止发送")
        return (
            f"工具结果过大（{len(text):,} 字符）且快照写入失败，已阻止发送。原因：{exc.user_message}",
            True,
        )
    notify(f"工具结果过大（{len(text):,} 字符），完整内容已写入：{path}")
    preview = text[: cfg.preview_chars]
    return (f"{preview}\n…（已截断显示，完整内容已写入：{path}）", False)


def apply_prevention(results: dict, tool_calls, config, notify, save=None) -> dict:
    """预防层：就地改写本批工具结果（返回新的 results 字典）。

    第一遍处理单条超限；第二遍在合计仍超限时按从大到小依次存盘。
    未超限的结果一字不动；写盘失败的结果标记为错误（回退到阻止发送）。
    """
    cfg = config if isinstance(config, _CompactConfig) else _config_from(config)
    save = save or save_snapshot
    results = dict(results)

    # 第一遍：单条超限
    processed: set[str] = set()
    for tc in tool_calls:
        text, is_error = results[tc.id]
        if not is_error and len(text) > cfg.tool_result_chars:
            processed.add(tc.id)
            results[tc.id] = _shrink(text, tc.name, cfg, notify, save)

    # 第二遍：合计超限 → 挑大的依次存盘。每条至多处理一次：
    # 预览文本（含快照路径）本身有长度，反复收缩不会收敛，只会无限写盘。
    def total_size() -> int:
        return sum(len(results[tc.id][0]) for tc in tool_calls if not results[tc.id][1])

    while total_size() > cfg.message_chars:
        candidates = [
            tc
            for tc in tool_calls
            if tc.id not in processed
            and not results[tc.id][1]
            and len(results[tc.id][0]) > cfg.preview_chars
        ]
        if not candidates:
            break
        biggest = max(candidates, key=lambda tc: len(results[tc.id][0]))
        processed.add(biggest.id)
        results[biggest.id] = _shrink(results[biggest.id][0], biggest.name, cfg, notify, save)

    return results


def _user_turn_starts(messages: Sequence[Message]) -> list[int]:
    """真实用户消息（非工具结果）在消息列表中的索引。"""
    return [
        i for i, m in enumerate(messages) if m.role is Role.USER and m.tool_call_id is None
    ]


def _user_quotes(messages: Sequence[Message]) -> str:
    """用户原话逐字拼接——由代码保证，不依赖模型改写。"""
    quotes = [
        m.content
        for m in messages
        if m.role is Role.USER and m.tool_call_id is None
    ]
    return "\n".join(f"- {quote}" for quote in quotes) if quotes else "（无）"


def compact_history(
    session,
    provider,
    model: str,
    notify: Callable[[str], None],
    breaker: CompactBreaker,
    force: bool = False,
) -> bool:
    """兜底层：生成摘要并替换较早轮次。返回是否发生了压缩。

    force=True 时绕过熔断与阈值（手动触发）；失败计数无论是否 force 都照记。
    """
    if not force and breaker.open:
        return False  # 熔断中：自动触发停止（手动 force 绕过）
    messages = session.messages()
    starts = _user_turn_starts(messages)
    if len(starts) <= KEEP_RECENT_TURNS:
        return False  # 轮次太少，没有可压缩的

    cut = starts[-KEEP_RECENT_TURNS]
    older, recent = messages[:cut], messages[cut:]
    if not older:
        return False

    try:
        model_summary = summarizer.summarize(provider, model, older)
    except EikoCodeError as exc:
        breaker.record_failure()
        if breaker.open:
            notify(
                f"摘要连续失败 {breaker.failures} 次，已熔断：自动兜底停止"
                f"（/compact 仍可手动触发）。最后一次原因：{exc.user_message}"
            )
        else:
            notify(
                f"摘要生成失败（{breaker.failures}/{breaker.max_failures}）：{exc.user_message}"
            )
        return False
    breaker.record_success()

    full = (
        f"{model_summary}\n\n## 用户原话（逐字保留）\n{_user_quotes(older)}\n\n{BOUNDARY_MESSAGE}"
    )
    session.rewrite([Message(role=Role.USER, content=full), *recent])
    notify(
        f"已压缩较早对话：{len(older)} 条消息 → 1 条摘要"
        f"（保留最近 {KEEP_RECENT_TURNS} 轮原文）"
    )
    return True
