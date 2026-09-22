"""命令解析器（v9）：回车输入的分流器。

剥除可能随粘贴混入的不可见字符（BOM 等），识别斜杠前缀；首个空白之前
是命令名（转小写做到大小写不敏感），其余是参数。非命令输入标记为
「送对话流」（spec.md §3 能力 67）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedInput:
    is_command: bool
    name: str = ""    # 斜杠后、首个空白前的命令名（已转小写）
    arg: str = ""     # 命令名之后的参数（已 strip）
    text: str = ""    # 非命令输入时的原文


def parse(raw: str) -> ParsedInput:
    text = (raw or "").strip().lstrip("\ufeff")
    if not text.startswith("/"):
        return ParsedInput(is_command=False, text=text)
    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return ParsedInput(is_command=True, name=name, arg=arg)
