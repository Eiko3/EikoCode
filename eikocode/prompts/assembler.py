"""拼装器（v4）：按优先级从高到低，把指令模块拼成一条稳定系统消息。

拼装必须是**确定性**的：同一模块序列两次拼装的结果逐字节一致。
这是稳定前缀（缓存根基）的最后一道保证——所以这里只做纯字符串串联，
不读环境、不取时间、不碰任何会变化的东西。
"""

from __future__ import annotations

from typing import Sequence

from .modules import MODULE_SEQUENCE


def assemble(modules: Sequence[str] = MODULE_SEQUENCE) -> str:
    """把模块序列拼装为一条系统消息文本。

    模块之间以空行分隔；各模块首尾的空白被归一，保证内容不变时
    产物逐字节稳定。
    """
    parts = [m.strip() for m in modules if m and m.strip()]
    return "\n\n".join(parts)
