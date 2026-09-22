"""安全检查（v5）：纵深防御决策流水。

五层顺序求值：危险黑名单 → 路径沙箱 → 显式规则 → 权限档位 → 人在回路；
任何一层作出裁决即终止。裁决不是布尔值，而是「动作 + 依据」——依据
必须可见，不做静默裁决（spec.md §3 能力 42）。

明示边界：路径沙箱只覆盖文件类工具；命令执行类工具的文本无法被可靠
静态解析（变量拼接、别名、编码执行），不做路径检查，由黑名单与确认
流程兜底（spec.md §6「v5 本章明确不做」）。
"""

from .decision import ACTION_ALLOW, ACTION_ASK, ACTION_DENY, Decision
from .pipeline import SecurityPipeline

__all__ = [
    "ACTION_ALLOW",
    "ACTION_ASK",
    "ACTION_DENY",
    "Decision",
    "SecurityPipeline",
]
