"""成员协作工具组（v14）：task_* 与 send_message，仅成员实例可见。

主对话与 v12 普通子工作者的注册表不注册这些工具（spec.md §3 能力 113）。
"""

from __future__ import annotations

from ..tools.base import Tool


class TeamContextToolBase(Tool):
    """协作工具基类：持有小组上下文。"""

    def __init__(self, team, member_name: str) -> None:
        self._team = team
        self._me = member_name


class TaskCreateTool(TeamContextToolBase):
    name = "task_create"
    description = "在小组共享清单中创建一个任务；depends_on 列出必须先完成的任务 id"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题"},
            "assignee": {"type": "string", "description": "指定成员名（可空 = 自由认领）"},
            "depends_on": {"type": "array", "items": {"type": "string"},
                            "description": "必须先完成的任务 id 列表"},
        },
        "required": ["title"],
    }

    def execute(self, arguments):
        tid = self._team.create_task(
            str(arguments.get("title", "")),
            assignee=str(arguments.get("assignee", "")),
            depends_on=arguments.get("depends_on") or [],
        )
        return f"任务已创建：{tid}"


class TaskViewTool(TeamContextToolBase):
    name = "task_view"
    description = "查看共享清单中一个任务的详情"
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "任务 id"}},
        "required": ["id"],
    }

    def execute(self, arguments):
        task = self._team.tasks.get(str(arguments.get("id", "")))
        if task is None:
            return "未知任务 id"
        return "\n".join(f"{k}: {v}" for k, v in task.items())


class TaskListTool(TeamContextToolBase):
    name = "task_list"
    description = "列出小组共享任务清单"

    def execute(self, arguments):
        return self._team.render_tasks()


class TaskUpdateTool(TeamContextToolBase):
    name = "task_update"
    description = "更新任务状态（待认领 / 进行中 / 完成）或结果；依赖未完成时不能标记完成"
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "任务 id"},
            "status": {"type": "string", "description": "新状态"},
            "result": {"type": "string", "description": "结果说明"},
        },
        "required": ["id"],
    }

    def execute(self, arguments):
        tid = str(arguments.get("id", ""))
        fields = {k: v for k, v in arguments.items() if k in ("status", "result") and v}
        err = self._team.update_task(tid, **fields)
        return err or f"任务 {tid} 已更新"


class SendMessageTool(TeamContextToolBase):
    name = "send_message"
    description = (
        "向小组成员或 Lead 发送消息；to 填成员名、'lead' 或 '*'（广播）。"
        "summary 是给接收方的一行摘要。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "成员名 / lead / *"},
            "text": {"type": "string", "description": "消息正文"},
            "summary": {"type": "string", "description": "一行摘要"},
        },
        "required": ["to", "text"],
    }

    def __init__(self, team, member_name: str, mailbox) -> None:
        super().__init__(team, member_name)
        self._mailbox = mailbox

    def execute(self, arguments):
        err = self._mailbox.send(
            to=str(arguments.get("to", "")),
            sender=self._me,
            text=str(arguments.get("text", "")),
            summary=str(arguments.get("summary", "")),
        )
        return err or "消息已投递"


COLLAB_TOOL_NAMES = ("task_create", "task_view", "task_list", "task_update", "send_message")


def build_collab_tools(team, member_name: str, mailbox) -> list[Tool]:
    """为一个成员实例装配协作工具组。"""
    return [
        TaskCreateTool(team, member_name),
        TaskViewTool(team, member_name),
        TaskListTool(team, member_name),
        TaskUpdateTool(team, member_name),
        SendMessageTool(team, member_name, mailbox),
    ]
