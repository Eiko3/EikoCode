"""环境探测与环境消息（v4）。

环境信息（工作目录 / 操作系统 / 日期）**不进系统消息**——它们会变化，
进稳定通道会自毁缓存。它们以带标签的补充消息作为对话首条注入；会话中
探测到变化时生成**新的**环境消息追加到请求末尾，不改写任何已发出的
历史消息（见 spec.md §3 能力 31）。

cwd 由调用方传入：Shell 工具的工作目录在工具实例内延续，主进程的
`Path.cwd()` 感知不到，所以运行时从注册表取 Shell 工具的当前目录。
不探测 Git 状态（Git 集成本身在 Out of Scope）。
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ENV_OPEN = "<environment>"
ENV_CLOSE = "</environment>"


def _today() -> str:
    """拆成小函数便于测试替换（真实实现只读当天日期）。"""
    return date.today().isoformat()


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """一次环境探测的快照。快照相等 = 环境未变化。"""

    cwd: str
    os_name: str
    date: str


def capture(cwd: str | None = None) -> EnvironmentSnapshot:
    """探测当前环境。cwd 缺省时取主进程工作目录。"""
    return EnvironmentSnapshot(
        cwd=cwd or str(Path.cwd()),
        os_name=f"{platform.system()} {platform.release()}",
        date=_today(),
    )


def format_message(snapshot: EnvironmentSnapshot) -> str:
    """把快照格式化为带标签的环境补充消息（user 角色的对话消息）。"""
    return (
        f"{ENV_OPEN}\n"
        f"工作目录：{snapshot.cwd}\n"
        f"操作系统：{snapshot.os_name}\n"
        f"日期：{snapshot.date}\n"
        f"{ENV_CLOSE}"
    )
