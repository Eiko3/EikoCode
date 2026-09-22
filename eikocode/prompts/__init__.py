"""提示词装配（v4）：指令模块、拼装器、环境消息、运行时注入。

本包只产出「文本素材」：稳定系统消息（模块拼装）、环境补充消息、
带标签的注入指令。发往供应商前的最终组装由代理运行时完成（见
`agent/loop.py`）；本包不触碰会话层与呈现层。

稳定 / 变化分离（见 spec.md §3 能力 30）：
- 稳定前缀 = 系统消息 + 工具声明，会话内逐字节不变——缓存生效的根基；
- 变化内容 = 环境首条 / 环境追加 / 会话历史 / 当轮注入，只追加不改写。
"""

from .assembler import assemble
from .environment import EnvironmentSnapshot, capture, format_message
from .injection import (
    PLAN_BRIEF,
    PLAN_FULL,
    PLAN_FULL_INTERVAL,
    plan_reminder,
    reminder,
)

__all__ = [
    "EnvironmentSnapshot",
    "PLAN_BRIEF",
    "PLAN_FULL",
    "PLAN_FULL_INTERVAL",
    "assemble",
    "capture",
    "format_message",
    "plan_reminder",
    "reminder",
]
