"""上下文用量估算与阈值判断。

只是估算，不是精确计数：CJK 字符按 1 个 token 算，其余按 4 个字符 1 个 token。
估算偏高一点是好事——宁可早提醒，也别等 API 报错。
"""

from __future__ import annotations

import math
from enum import IntEnum

WARN_RATIO = 0.80
BLOCK_RATIO = 0.95

# 每条消息的角色标记与分隔开销
PER_MESSAGE_OVERHEAD = 4

WARN_MESSAGE = "上下文用量已达 80%；达到 90% 将自动压缩较早对话（/compact 可手动压缩）。"
BLOCK_MESSAGE = (
    "上下文用量已达 95%，自动压缩后仍超限，本次请求未发送。请输入 /clear 开启新会话。"
)


class Level(IntEnum):
    OK = 0
    WARN = 1
    BLOCK = 2


def _is_wide(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    wide = sum(1 for char in text if _is_wide(char))
    narrow = len(text) - wide
    return wide + math.ceil(narrow / 4)


def estimate_messages(messages) -> int:
    return sum(estimate_tokens(m.content) + PER_MESSAGE_OVERHEAD for m in messages)


def ratio(used: int, limit: int) -> float:
    if limit <= 0:
        return 1.0
    return used / limit


def level_for(used: int, limit: int) -> Level:
    value = ratio(used, limit)
    if value >= BLOCK_RATIO:
        return Level.BLOCK
    if value >= WARN_RATIO:
        return Level.WARN
    return Level.OK
