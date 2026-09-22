"""危险操作黑名单（v5）：流水第一层。

命中 → 红色拦截 + 显式确认字样 `yes`（沿 v2 行为），**不受任何档位与
规则影响**（spec.md §3 能力 36）。黑名单是「已知高危」层而非安全边界
本身——对抗性变体（混淆、编码执行的新花样）不在防御范围（Out of Scope）。

v2 的模式表全部保留；v5 新增「下载即执行」类模式（提议默认清单，
见 checklist.md 组 35）。
"""

from __future__ import annotations

import re

# 危险模式表。命中任一项即触发红色拦截（不分大小写）。
_DANGEROUS_PATTERNS = [
    # -- v2 既有：破坏性文件与系统操作 -- #
    r"\brm\s+-rf\b",
    r"\brm\s+-fr\b",
    r"\brm\s+-r\b",  # PowerShell 的 rm 即 Remove-Item
    r"\bremove-item\b.*-recurse",
    r"\brmdir\s+/s\b",
    r"\bdel\s+/f\b",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\bdiskpart\b",
    r"\breg\s+delete\b",
    r"\btakeown\b",
    r"\bicacls\b.*\breset\b",
    r":\(\)\s*\{",        # fork bomb
    r"\\\\\.\\",          # 写物理盘 \\.\PhysicalDisk / \\.\C:
    # -- v5 新增：下载即执行 / 表达式执行（提议默认）-- #
    r"\binvoke-expression\b",
    r"\biex\b",           # Invoke-Expression 的别名，正常命令极少出现，宁可误伤
    r"-encodedcommand\b",  # 编码命令执行，是隐藏意图的惯用手法
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]


def contains_dangerous(command: str) -> bool:
    """命令文本是否命中危险模式表。空串不命中。"""
    lowered = (command or "").lower()
    return any(pat.search(lowered) for pat in _COMPILED)
