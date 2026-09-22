"""安全决策流水（v5）：把五层串起来。

顺序：危险黑名单 → 路径沙箱 → 显式规则 → 权限档位 → 人在回路；
任何一层作出裁决即终止。裁决是「动作 + 依据」，依据一律上终端
（ask 本身就是交互；deny / 非安静 allow 由调用方提示）。

显式规则的求值次序：会话临时 > 项目级 > 用户全局；显式拒绝在任何
档位下都拦、不进人在回路（spec.md §3 能力 41）。
"""

from __future__ import annotations

from pathlib import Path

from ..config import USER_CONFIG_PATH
from ..tools.base import PermissionLevel, Tool
from .blacklist import contains_dangerous
from .decision import ACTION_ASK, ACTION_DENY, Decision
from .hitl import resolve as resolve_hitl
from .modes import evaluate_mode
from .rules import (
    SOURCE_PROJECT,
    SOURCE_SESSION,
    SOURCE_USER,
    Rule,
    find_rule,
    match_value,
    parse_rules,
)
from .sandbox import PathSandbox


class SecurityPipeline:
    """一次工具调用的安全裁决入口。"""

    def __init__(
        self,
        config,
        session_rules: list[Rule] | None = None,
        base_dir: Path | None = None,
        user_config_path: Path | None = None,
    ) -> None:
        # getattr 兜底：测试里的极简假 config 可能没带这些字段。
        extra_dirs = list(getattr(config, "sandbox_dirs", ()) or ())
        self._sandbox = PathSandbox(extra_dirs, base_dir=base_dir)
        self._project_rules = parse_rules(
            getattr(config, "project_rules", ()) or (), SOURCE_PROJECT
        )
        self._user_rules = parse_rules(getattr(config, "user_rules", ()) or (), SOURCE_USER)
        self._session_rules = session_rules if session_rules is not None else []
        # 档位：显式 permission_mode 优先；缺失（假 config）时由 auto_approve 映射，
        # 与 config.load_config 的映射保持一致。
        mode = str(getattr(config, "permission_mode", "") or "")
        if not mode:
            mode = "permissive" if getattr(config, "auto_approve", False) else "default"
        self._mode = mode
        self._user_config_path = user_config_path or USER_CONFIG_PATH

    # -- 档位 --------------------------------------------------------------- #
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in ("strict", "default", "permissive"):
            raise ValueError(f"未知档位：{mode}")
        self._mode = mode

    # -- 求值 --------------------------------------------------------------- #
    def evaluate(self, tool: Tool, arguments: dict, ui, ask) -> Decision:
        """按五层顺序求值，返回最终裁决（ask 已在人在回路落定）。"""
        decision = self._by_blacklist(tool, arguments)
        if decision is None:
            decision = self._sandbox.check(arguments)
        if decision is None:
            decision = self._by_rules(tool, arguments)
        if decision is None:
            decision = evaluate_mode(self._mode, tool.permission is PermissionLevel.READ)

        if decision.action == ACTION_ASK:
            return resolve_hitl(
                decision,
                tool.name,
                arguments,
                ui,
                ask,
                self._session_rules,
                self._user_config_path,
            )
        return decision

    def _by_blacklist(self, tool: Tool, arguments: dict) -> Decision | None:
        command = (
            str(arguments.get("command", ""))
            if tool.permission is PermissionLevel.EXECUTE
            else ""
        )
        if tool.is_dangerous or contains_dangerous(command):
            return Decision(ACTION_ASK, "危险命令：命中高危模式黑名单", dangerous=True)
        return None

    def _by_rules(self, tool: Tool, arguments: dict) -> Decision | None:
        value = match_value(tool.name, arguments)
        for rules, source in (
            (self._session_rules, SOURCE_SESSION),
            (self._project_rules, SOURCE_PROJECT),
            (self._user_rules, SOURCE_USER),
        ):
            rule = find_rule(rules, tool.name, value)
            if rule is not None:
                return Decision(rule.action, f"命中规则（{source}）：{rule.action}")
        return None
