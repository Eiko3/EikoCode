"""Hook 系统（v11）：事件 + 条件 + 动作的声明式自动化。

对外入口：`load_hooks`（两级加载）与 `HookEngine`（事件求值）。
"""

from .engine import HookEngine
from .models import (
    DEFAULT_TIMEOUT,
    EVENTS,
    HOOKS_FILENAME,
    HookSpec,
    INTERCEPTABLE_EVENTS,
    parse_hooks,
)

__all__ = [
    "HookEngine",
    "HookSpec",
    "EVENTS",
    "INTERCEPTABLE_EVENTS",
    "HOOKS_FILENAME",
    "DEFAULT_TIMEOUT",
    "parse_hooks",
    "load_hooks",
]


def load_hooks(project_path=None, user_path=None, notice=lambda t: None):
    """两级加载 hooks.yaml：用户级先、项目级后，两级规则都执行。"""
    from .models import load_two_levels

    return load_two_levels(project_path, user_path, notice)
