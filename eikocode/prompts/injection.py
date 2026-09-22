"""运行时注入（v4）：带特殊标签的补充指令。

注入只作用于**当轮请求**（拼在消息列表末尾），不写入会话历史、不参与
上下文计量；标签语义（system-reminder）让模型把它当系统级提示而不是
用户输入。每次注入必须经 `_Ui.notice` 在终端可见，不静默操控模型。

会话级开关（只规划模式）的指令按轮次节奏注入：开启首轮发完整版，
此后每隔 `PLAN_FULL_INTERVAL` 轮重复一次完整版，其余轮次发精简版
（见 spec.md §3 能力 34，具体值见 checklist.md 组 30）。
"""

from __future__ import annotations

REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"

# 只规划模式完整版提醒。
PLAN_FULL = (
    "当前处于只规划模式：\n"
    "- 仅允许使用只读工具（读取文件、搜索内容）。\n"
    "- 写入与执行类工具会被拦截，不要尝试调用它们。\n"
    "- 请产出一份可执行的计划交用户审批，不要实际改动任何文件。"
)

# 只规划模式精简版提醒（其余轮次，防止模型「忘记」模式而刷屏）。
PLAN_BRIEF = "当前处于只规划模式：仅允许只读工具。"

# 完整版提醒的重复间隔（提议默认 5，见 checklist.md 组 30）。
PLAN_FULL_INTERVAL = 5


def reminder(text: str) -> str:
    """把补充指令包进特殊标签。"""
    return f"{REMINDER_OPEN}\n{text}\n{REMINDER_CLOSE}"


def plan_reminder(plan_turn: int) -> str:
    """只规划模式开启后第 `plan_turn` 轮（从 1 计）应注入的提醒。

    节奏：第 1 轮完整版；此后每 `PLAN_FULL_INTERVAL` 轮重复一次完整版
    （第 6、11、… 轮）；其余轮次精简版。
    """
    if plan_turn == 1 or plan_turn % PLAN_FULL_INTERVAL == 1:
        return reminder(PLAN_FULL)
    return reminder(PLAN_BRIEF)
