"""权限档位（v5）：流水第四层。

三档只对「未命中任何显式规则」的调用生效（提议默认矩阵，见
checklist.md 组 38）：

- strict：只读也逐条询问（全量确认，除黑名单红色确认外）
- default：v2 现状——只读自动、写与执行确认
- permissive：v4 自动批准——读 / 写 / 非危险命令直接过

黑名单命中不会到达本层（第一层已裁决），所以三档都不影响红色确认。
注意：strict 与 default 对只读工具的差别是本章实施期修正——提议默认
矩阵中两档的未命中行为原本相同，那会让 strict 名存实亡；现改为
strict 下只读也询问。
"""

from __future__ import annotations

from .decision import ACTION_ALLOW, ACTION_ASK, Decision

MODES = ("strict", "default", "permissive")

_MODE_LABEL = {"strict": "严格", "default": "默认", "permissive": "放行"}


def mode_label(mode: str) -> str:
    return _MODE_LABEL.get(mode, mode)


def evaluate_mode(mode: str, is_read_only: bool) -> Decision | None:
    """按档位对未命中规则的调用裁决。

    返回 None 表示本层不裁决（不会发生——档位是兜底层；保留 None 语义
    以防未来新增「继续下层」的档位行为）。
    """
    if mode == "permissive":
        return Decision(ACTION_ALLOW, "权限档位（放行）：自动批准", silent=not is_read_only)
    if mode == "strict":
        return Decision(ACTION_ASK, "权限档位（严格）：需确认")
    # default：只读自动，其余确认
    if is_read_only:
        return Decision(ACTION_ALLOW, "只读工具自动放行", silent=True)
    return Decision(ACTION_ASK, "权限档位（默认）：需确认")
