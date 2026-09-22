"""小组与成员模型（v14）：小组目录落盘与恢复。

小组目录 `<仓库>/.eikocode/teams/<名>/`：
- members.json   成员花名册（角色 / 工作目录 / 后端 / 实例 id / 状态）
- tasks.json     共享任务清单（含 depends_on 依赖）
- mailboxes/<实例id>/inbox.jsonl  点对点邮箱
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TEAMS_DIRNAME = "teams"


@dataclass
class MemberSpec:
    """一个小组成员的元信息。"""

    name: str
    role: str  # 子工作者角色名
    worktree: str = ""  # v13 工作树名；空 = 主目录
    backend: str = "inline"  # window | inline
    needs_approval: bool = True
    instance_id: str = ""  # 邮箱定位 id；空 = 未运行
    status: str = "未启动"  # 未启动 / 运行中 / 空闲 / 已终止


@dataclass
class Team:
    """长期存在的小组对象。"""

    name: str
    members: dict[str, MemberSpec] = field(default_factory=dict)
    tasks: dict[str, dict] = field(default_factory=dict)  # id → {title,status,assignee,depends_on,result}
    dir: Path | None = None

    @property
    def mailboxes_dir(self) -> Path:
        return self.dir / "mailboxes"

    # -- 持久化 ---------------------------------------------------------------- #
    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "members.json").write_text(
            json.dumps(
                {n: vars(m) for n, m in self.members.items()},
                ensure_ascii=False, indent=2, default=str,
            ),
            encoding="utf-8",
        )
        (self.dir / "tasks.json").write_text(
            json.dumps(self.tasks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, name: str, base_dir: Path) -> "Team | None":
        d = Path(base_dir) / TEAMS_DIRNAME / name
        members_file = d / "members.json"
        if not members_file.is_file():
            return None
        team = cls(name=name, dir=d)
        raw = json.loads(members_file.read_text(encoding="utf-8"))
        for n, m in raw.items():
            team.members[n] = MemberSpec(**m)
        tasks_file = d / "tasks.json"
        if tasks_file.is_file():
            team.tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        return team

    # -- 共享任务清单 ------------------------------------------------------------ #
    def create_task(self, title: str, assignee: str = "", depends_on: list[str] | None = None) -> str:
        tid = f"t{len(self.tasks) + 1}"
        self.tasks[tid] = {
            "title": title, "status": "待认领",
            "assignee": assignee, "depends_on": list(depends_on or []), "result": "",
        }
        self.save()
        return tid

    def update_task(self, tid: str, **fields) -> str | None:
        """更新任务字段。依赖未完成时拒绝标记完成，返回错误原因。"""
        task = self.tasks.get(tid)
        if task is None:
            return f"未知任务：{tid}"
        if fields.get("status") == "完成":
            for dep in task.get("depends_on", []):
                dep_task = self.tasks.get(dep)
                if dep_task and dep_task.get("status") != "完成":
                    return f"任务 {tid} 的依赖 {dep} 尚未完成，不能标记完成"
        task.update(fields)
        self.save()
        return None

    def render_tasks(self) -> str:
        lines = []
        for tid, task in self.tasks.items():
            deps = f"（依赖：{', '.join(task['depends_on'])}）" if task.get("depends_on") else ""
            lines.append(f"{tid} [{task['status']}] {task['title']} @{task.get('assignee') or '自由'}{deps}")
        return "\n".join(lines) or "（清单为空）"
