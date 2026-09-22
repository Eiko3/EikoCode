"""上下文压缩（v7）：阈值估算（threshold）、预防层与兜底层。

每次请求前按序执行（spec.md §3 能力 56）：
1. 预防层（轻量，无 LLM）：单个工具结果超阈值 → 全文写盘、消息留预览与路径；
   同一条消息内多个工具结果合计超限 → 挑大的依次存盘。
2. 兜底层（调 LLM）：累计用量逼近窗口上限 → 生成结构化摘要替换较早轮次，
   最近数轮保持原文；用户原话由代码逐字拼接，不被模型改写。

明示边界（spec.md §6「v7 本章明确不做」）：只压缩会话历史；快照只创建
不清理；一次兜底到位，失败即熔断。
"""

# v1 的阈值估算模块随本包提供（原 eikocode/context.py），保持既有 import 兼容
from .threshold import (  # noqa: F401
    BLOCK_MESSAGE,
    BLOCK_RATIO,
    PER_MESSAGE_OVERHEAD,
    WARN_MESSAGE,
    WARN_RATIO,
    Level,
    estimate_messages,
    estimate_tokens,
    level_for,
    ratio,
)

from .compaction import (  # noqa: F401
    BOUNDARY_MESSAGE,
    COMPACT_RATIO,
    CompactBreaker,
    apply_prevention,
    compact_history,
)
from .snapshots import save_snapshot  # noqa: F401
from .summarizer import SUMMARY_SECTIONS, build_prompt, parse_summary  # noqa: F401

__all__ = [
    "BLOCK_MESSAGE",
    "BLOCK_RATIO",
    "BOUNDARY_MESSAGE",
    "COMPACT_RATIO",
    "PER_MESSAGE_OVERHEAD",
    "SUMMARY_SECTIONS",
    "CompactBreaker",
    "Level",
    "WARN_MESSAGE",
    "WARN_RATIO",
    "apply_prevention",
    "build_prompt",
    "compact_history",
    "estimate_messages",
    "estimate_tokens",
    "level_for",
    "parse_summary",
    "ratio",
    "save_snapshot",
]
