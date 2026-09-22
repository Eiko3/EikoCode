"""Hook 数据模型与规则校验（v11）。

每条 Hook = 事件 + 条件（可省略）+ 动作，外加执行控制三件套。
加载时集中校验：事件名、动作类型与必填字段；非法规则定位到
「来源 + 序号 + 原因」并跳过，不阻断其余规则（spec.md §3 能力 83、90）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .yaml_mini import YamlParseError, parse_document

# 12 种生命周期 / 系统级事件（spec.md §3 能力 84）
EVENTS = frozenset({
    "session_start", "session_end",
    "turn_start", "turn_end",
    "message_send", "message_receive",
    "tool_before", "tool_after",
    "startup", "shutdown", "error", "compaction",
})

# 拦截语义只作用于这些事件（提议默认：仅工具执行前，见 checklist 组 86）
INTERCEPTABLE_EVENTS = frozenset({"tool_before"})

# 动作类型与必填字段（spec.md §3 能力 87）
ACTION_TYPES = ("shell", "prompt", "http", "subagent")
ACTION_REQUIRED = {"shell": ("command",), "prompt": ("text",), "http": ("url",), "subagent": ()}

# 条件操作符与逻辑组合（spec.md §3 能力 86）
OPERATORS = ("eq", "not", "glob", "regex")
MATCH_MODES = ("all", "any")

# hook 动作超时缺省（提议默认，见 checklist 组 85）
DEFAULT_TIMEOUT = 10

# hooks 声明文件名
HOOKS_FILENAME = "hooks.yaml"


@dataclass
class HookSpec:
    """一条已校验的 Hook 规则。"""

    event: str
    action: dict  # {"type": ..., 其他字段...}
    conditions: dict | None = None  # {match?: all|any, 字段: 值|{op: 值}}
    once: bool = False
    async_: bool = False
    timeout: int = DEFAULT_TIMEOUT
    source: str = ""  # 来源描述（报错与拦截依据展示用）
    index: int = 0  # 来源内序号（1 起），错误定位用

    @property
    def is_interceptable(self) -> bool:
        return self.event in INTERCEPTABLE_EVENTS

    @property
    def action_type(self) -> str:
        return str(self.action.get("type", ""))


def _validate_rule(item: dict, source: str, index: int) -> tuple[HookSpec | None, str | None]:
    """校验单条规则。返回 (spec, None) 或 (None, 错误原因)。"""
    label = f"{source} 第 {index} 条规则"
    if not isinstance(item, dict):
        return None, f"{label}：不是映射结构"

    event = str(item.get("event", "")).strip()
    if not event:
        return None, f"{label}：缺少 event"
    if event not in EVENTS:
        return None, f"{label}：未知事件「{event}」（可用：{', '.join(sorted(EVENTS))}）"

    action = item.get("action")
    if not isinstance(action, dict):
        return None, f"{label}：缺少 action 或不是映射结构"
    action_type = str(action.get("type", "")).strip()
    if action_type not in ACTION_TYPES:
        return None, f"{label}：未知动作类型「{action_type}」（可用：{', '.join(ACTION_TYPES)}）"
    missing = [k for k in ACTION_REQUIRED[action_type] if not str(action.get(k, "")).strip()]
    if missing:
        return None, f"{label}：动作 {action_type} 缺少必填字段 {', '.join(missing)}"

    conditions = item.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, dict):
            return None, f"{label}：conditions 应为映射结构"
        match = str(conditions.get("match", "all")).strip().lower()
        if match not in MATCH_MODES:
            return None, f"{label}：conditions.match 只能是 all 或 any，不允许混用"
        if "and" in conditions or "or" in conditions:
            return None, f"{label}：不支持 and / or 混用组合，只能用 match: all | any"

    try:
        timeout = int(item.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return None, f"{label}：timeout 应为整数秒"

    return (
        HookSpec(
            event=event,
            action=action,
            conditions=conditions if isinstance(conditions, dict) else None,
            once=bool(item.get("once", False)),
            async_=bool(item.get("async", False)),
            timeout=timeout,
            source=source,
            index=index,
        ),
        None,
    )


def parse_hooks(text: str, source: str) -> tuple[list[HookSpec], list[str]]:
    """把 hooks.yaml 文本解析为规则列表。

    返回（合法规则, 错误清单）；顶层结构必须是含 hooks 数组的映射，
    单条规则非法不阻断其余。
    """
    try:
        data = parse_document(text)
    except YamlParseError as exc:
        return [], [f"{source}：YAML 解析失败：{exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), list):
        return [], [f"{source}：顶层应为含 hooks 数组的映射"]
    specs: list[HookSpec] = []
    errors: list[str] = []
    for i, item in enumerate(data["hooks"], start=1):
        spec, err = _validate_rule(item, source, i)
        if err:
            errors.append(err)
        else:
            specs.append(spec)
    return specs, errors


def load_two_levels(
    project_path: Path | None, user_path: Path | None, notice=lambda t: None
) -> tuple[list[HookSpec], list[str]]:
    """两级加载：用户级先、项目级后，两级规则都执行（行为叠加非覆盖）。

    无文件为正常态；单文件解析失败逐条定位。
    """
    specs: list[HookSpec] = []
    errors: list[str] = []
    for path, label in (
        (user_path, "用户级 hooks"),
        (project_path, "项目级 hooks"),
    ):
        if path is None or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{label}：读取失败 {exc}")
            continue
        s, e = parse_hooks(text, label)
        specs.extend(s)
        errors.extend(e)
    return specs, errors
