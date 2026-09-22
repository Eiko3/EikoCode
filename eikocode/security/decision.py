"""安全裁决类型（v5）。

流水各层产出 `Decision`；`reason` 是给用户看的依据，任何裁决都必须
携带（无依据的静默裁决是禁止的）。`silent` 标记「不需要终端提示的
放行」——只读工具自动通过沿用 v2 以来的安静行为，不算静默裁决，
因为那是既有约定而非被隐藏的决定。
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_ALLOW = "allow"
ACTION_DENY = "deny"
ACTION_ASK = "ask"


@dataclass(frozen=True)
class Decision:
    """一次安全裁决：动作 + 依据。"""

    action: str  # ACTION_ALLOW / ACTION_DENY / ACTION_ASK
    reason: str  # 依据文本，给用户看
    dangerous: bool = False  # ask 的红色确认变体（黑名单命中），无三范围选项
    silent: bool = False  # allow 的安静变体（只读自动通过）

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("安全裁决必须携带依据（reason 非空）")
