"""Hook 条件求值器（v11）：字段取值 + 四操作符 + all/any 组合。

字段：tool（工具名）、args.<key>（工具参数字段）、message（消息 / 结果文本）、
error（错误信息）、cwd（工作目录）。标量条件 = 精确匹配（eq）；
`{op: 值}` 支持 eq / not / glob / regex。组合 match 只能 all 或 any
二选一（校验层已拒绝混用），不引入表达式引擎（spec.md §3 能力 86）。
"""

from __future__ import annotations

import re
from fnmatch import fnmatch

# 事件上下文里允许取值的顶层字段（其余字段名视为未定义 → 空串）
_FIELDS = ("tool", "message", "error", "cwd", "event")


def _field_value(field: str, context: dict) -> str:
    """取条件字段值；args.<key> 深入工具参数；未定义 → 空串不报错。"""
    if field.startswith("args."):
        args = context.get("args")
        key = field[5:]
        if isinstance(args, dict) and key in args:
            return str(args[key])
        return ""
    if field in _FIELDS:
        value = context.get(field)
        return "" if value is None else str(value)
    return ""


def _match_op(value: str, cond) -> bool:
    """单条件求值：标量 = eq；dict = {op: 值}（多 op 并存按 all 求值）。"""
    if isinstance(cond, dict):
        results = []
        for op, pattern in cond.items():
            op = str(op).strip().lower()
            pattern = str(pattern)
            if op == "eq":
                results.append(value == pattern)
            elif op == "not":
                results.append(value != pattern)
            elif op == "glob":
                results.append(fnmatch(value, pattern))
            elif op == "regex":
                try:
                    results.append(re.search(pattern, value) is not None)
                except re.error:
                    results.append(False)  # 非法正则按不命中（校验层已尽量前置拦截）
            else:
                results.append(False)
        return all(results)
    return value == str(cond)


def evaluate_conditions(conditions: dict | None, context: dict) -> bool:
    """求值条件块。None = 无条件触发；match 决定 all / any（缺省 all）。"""
    if not conditions:
        return True
    match_mode = str(conditions.get("match", "all")).strip().lower()
    checks = [(k, v) for k, v in conditions.items() if k != "match"]
    if not checks:
        return True
    results = [_match_op(_field_value(k, context), v) for k, v in checks]
    return all(results) if match_mode == "all" else any(results)
