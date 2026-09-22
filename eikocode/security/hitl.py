"""人在回路（v5）：流水第五层。

规则与档位都给出「询问」时，把决定权交回用户（spec.md §3 能力 40）：

- 普通询问：三范围选择——本次放行 / 本会话放行（生成会话临时规则）/ 永久放行
  （写入用户级配置，写前明示）；
- 危险询问（黑名单命中）：沿用 v2 红色确认，必须输入完整 `yes`，无三范围选项、
  不生成任何规则。
"""

from __future__ import annotations

from pathlib import Path

from .decision import ACTION_ALLOW, ACTION_DENY, Decision
from .rules import Rule, SOURCE_SESSION, SOURCE_USER, match_value

# 用户级配置路径（config.py 定义权威值，这里由 pipeline 注入，便于测试替换）。


def resolve(
    decision: Decision,
    tool_name: str,
    arguments: dict,
    ui,
    ask,
    session_rules: list[Rule],
    user_config_path: Path,
) -> Decision:
    """处理一次询问。ui / ask 是交互回调；session_rules 会被就地追加。"""
    if decision.dangerous:
        return _dangerous_confirm(tool_name, arguments, ui, ask)
    return _normal_confirm(tool_name, arguments, ui, ask, session_rules, user_config_path)


def _dangerous_confirm(tool_name: str, arguments: dict, ui, ask) -> Decision:
    command = str(arguments.get("command", ""))
    ui.error("⚠ 危险命令：该操作可能破坏系统或删除数据。")
    if command:
        ui.error(f"  命令：{command}")
    ui.error("  若确认执行，请输入 yes；其它任何输入都会取消。")
    resp = ask("确认执行危险命令？输入 yes 继续：").strip()
    if resp == "yes":
        return Decision(ACTION_ALLOW, "用户显式确认执行危险命令")
    return Decision(ACTION_DENY, "用户拒绝执行")


def _normal_confirm(
    tool_name: str,
    arguments: dict,
    ui,
    ask,
    session_rules: list[Rule],
    user_config_path: Path,
) -> Decision:
    resp = ask(
        f"允许执行工具 {tool_name} 吗？[y=本次 / s=本会话 / a=永久 / 其它拒绝] "
    ).strip().lower()

    if resp == "y":
        return Decision(ACTION_ALLOW, "用户选择：本次放行")

    if resp == "s":
        value = match_value(tool_name, arguments)
        rule = Rule(tool=tool_name, match=value or "*", action="allow", source=SOURCE_SESSION)
        session_rules.append(rule)
        ui.notice(f"已加入本会话允许规则：{rule.tool} 匹配 {rule.match}")
        return Decision(ACTION_ALLOW, "用户选择：本会话放行（已生成会话临时规则）")

    if resp == "a":
        value = match_value(tool_name, arguments)
        rule = Rule(tool=tool_name, match=value or "*", action="allow", source=SOURCE_USER)
        ui.notice(f"即将写入用户级配置 {user_config_path}：tool={rule.tool} match={rule.match} action=allow")
        try:
            _append_user_rule(user_config_path, rule)
            ui.notice("已写入用户级配置，跨会话生效。")
        except OSError as exc:
            ui.notice(f"写入用户级配置失败（{type(exc).__name__}），本次仍按你的选择放行。")
        return Decision(ACTION_ALLOW, "用户选择：永久放行")

    return Decision(ACTION_DENY, "用户拒绝执行")


def _append_user_rule(path: Path, rule: Rule) -> None:
    """把规则追加到用户级配置末尾（不重写既有内容，最小侵入）。"""
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    block = (
        f'\n[[rules]]\n'
        f'tool = "{esc(rule.tool)}"\n'
        f'match = "{esc(rule.match)}"\n'
        f'action = "{rule.action}"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(block)
