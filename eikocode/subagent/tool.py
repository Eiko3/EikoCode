"""Agent 工具（v12）：子工作者的单一工具入口。

工具列表不随角色增减变化——角色只在调用参数中选择。
role 缺省 = Fork 式（继承父会话，强制后台）；background: true 或 Fork
一律后台执行，前台结果同步回写。
"""

from __future__ import annotations

from typing import Callable

from ..tools.base import Tool


class AgentTool(Tool):
    """启动子工作者的系统级入口。"""

    name = "Agent"
    description = (
        "启动一个子工作者执行独立任务：指定 role 时以预定义角色在空白会话中执行；"
        "不指定 role 时以 Fork 模式继承当前对话上下文执行。"
        "默认前台执行（结果直接返回）；background: true 时转后台，完成后自动通知。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "预定义角色名（缺省 = Fork 继承当前上下文）"},
            "task": {"type": "string", "description": "要交给子工作者的完整任务描述"},
            "background": {"type": "boolean", "description": "是否后台运行（缺省 false）"},
        },
        "required": ["task"],
    }

    def __init__(self, on_start: Callable[[str | None, str, bool], str]) -> None:
        self._on_start = on_start

    def execute(self, arguments: dict) -> str:
        role = arguments.get("role") or None
        task = str(arguments.get("task") or "").strip()
        background = bool(arguments.get("background", False))
        if not task:
            return "错误：缺少 task 参数（要交给子工作者的任务描述）。"
        return self._on_start(role, task, background)
