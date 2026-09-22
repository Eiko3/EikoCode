"""显式规则引擎（v5）：流水第三层。

按「工具 + 参数值 glob 模式」声明允许 / 拒绝 / 询问（提议默认格式见
checklist.md 组 37）。三级来源求值次序：会话临时 > 项目级 > 用户全局；
高级来源命中即终止，不再看低级。显式拒绝不提供豁免——它排在档位之前，
任何档位下都拦（spec.md §3 能力 38、41）。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Sequence

SOURCE_SESSION = "会话临时"
SOURCE_PROJECT = "项目级"
SOURCE_USER = "用户全局"

ACTIONS = ("allow", "deny", "ask")


@dataclass(frozen=True)
class Rule:
    """一条声明式规则。"""

    tool: str  # 工具名，或 "*" 匹配任意工具
    match: str  # 参数值 glob 模式
    action: str  # allow / deny / ask
    source: str  # 来源标记（会话临时 / 项目级 / 用户全局），供依据展示


def parse_rules(items, source: str) -> list[Rule]:
    """把配置里的 rules 数组解析为规则列表；格式非法的条目跳过。"""
    rules: list[Rule] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", "")).strip()
        match = str(item.get("match", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        if not tool or not match or action not in ACTIONS:
            continue
        rules.append(Rule(tool=tool, match=match, action=action, source=source))
    return rules


def match_value(tool_name: str, arguments: dict) -> str:
    """规则与沙箱共用的匹配值：文件工具取路径参数，命令工具取命令文本。"""
    if "path" in arguments:
        return str(arguments["path"])
    if "command" in arguments:
        return str(arguments["command"])
    return ""


def find_rule(rules: Sequence[Rule], tool_name: str, value: str) -> Rule | None:
    """返回本来源中第一条命中的规则；无命中返回 None。"""
    for rule in rules:
        if rule.tool != "*" and rule.tool != tool_name:
            continue
        if fnmatch(value, rule.match):
            return rule
    return None
