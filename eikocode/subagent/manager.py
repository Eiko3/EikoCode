"""后台任务管理器（v12）：三进入路径、移交不重启、完成通知注入。

- 显式 background / 前台超阈值自动转后台（同一线程继续，移交不重启）/
  Fork 强制后台。
- 任务线程结束时把结构化通知推入主对话注入队列（下一轮请求生效）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..agent.loop import _Ui

# 前台等待阈值（秒，提议默认；超时自动转后台，见 checklist 组 93）
FOREGROUND_WAIT_SECONDS = 60


@dataclass
class BackgroundTask:
    """一个子工作者任务的运行时记录。"""

    id: str
    role: str  # 角色名或 "fork"
    status: str = "运行中"  # 运行中 / 完成 / 失败 / 已终止
    result: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    finished_at: str = ""
    tokens_estimate: int = 0
    thread: threading.Thread | None = None
    cancel: object = None  # CancelToken（协作终止）

    def summary_line(self) -> str:
        return f"{self.id}  {self.role}  {self.status}  {self.tokens_estimate:,} tokens  起 {self.started_at}"


class TaskManager:
    """进程级后台任务登记与通知。"""

    def __init__(self, notice: Callable[[str], None]) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notice = notice
        self._runtime = None  # bind 后指向主运行时（通知注入）

    def bind(self, runtime) -> None:
        self._runtime = runtime

    def push_injection(self, text: str) -> None:
        """完成通知进入主对话注入队列（下一轮请求生效）。"""
        if self._runtime is not None and hasattr(self._runtime, "push_injection"):
            self._runtime.push_injection(text)

    # -- 提交与执行 ----------------------------------------------------------- #
    def submit(self, role: str, runner_fn, background: bool) -> tuple[str, bool]:
        """启动任务线程。返回 (任务 id, 是否后台)。

        runner_fn(cancel) 在线程内执行并返回结果文本；
        background=False 时前台等待 FOREGROUND_WAIT_SECONDS，超时转后台
        （线程继续跑，移交不重启）。
        """
        task_id = uuid.uuid4().hex[:8]
        from ..agent import CancelToken

        token = CancelToken()
        task = BackgroundTask(id=task_id, role=role, cancel=token)
        self._tasks[task_id] = task

        done = threading.Event()

        def _worker() -> None:
            try:
                result = runner_fn(token)
                task.result = result
                task.status = "完成"
            except Exception as exc:  # 错误隔离：后台失败不影响主流程
                task.result = f"子工作者执行失败：{type(exc).__name__}"
                task.status = "失败"
            task.finished_at = datetime.now().strftime("%H:%M:%S")
            done.set()
            self._notify_done(task)

        thread = threading.Thread(target=_worker, daemon=True)
        task.thread = thread
        thread.start()

        if background:
            self._notice(f"子工作者已转后台（任务 {task_id}）：/tasks 查看，/tasks {task_id} 看详情")
            return task_id, True

        # 前台等待；超时自动转后台（线程继续跑完，移交不重启）
        if done.wait(timeout=FOREGROUND_WAIT_SECONDS):
            return task_id, False
        self._notice(f"前台任务超时（{FOREGROUND_WAIT_SECONDS} 秒），已自动转后台（任务 {task_id}），完成后会通知。")
        return task_id, True

    def run_foreground(self, role: str, runner_fn) -> str:
        """同步执行并直接返回结果（供单测与无需后台语义的调用方使用）。"""
        task_id, went_background = self.submit(role, runner_fn, background=False)
        if not went_background:
            return self._tasks[task_id].result
        # 转后台：等待线程最终完成再返回（仅在调用方坚持同步时）
        task = self._tasks[task_id]
        if task.thread is not None:
            task.thread.join()
        return task.result

    def _notify_done(self, task: BackgroundTask) -> None:
        summary = task.result[:200]
        text = (
            f"【后台任务完成】{task.id}（{task.role}，{task.status}）\n结果摘要：{summary}"
        )
        self._notice(text)
        self.push_injection(f"（系统提示：后台任务 {task.id}（{task.role}）已{task.status}，"
                            f"用 /tasks {task.id} 查看完整结果。）")

    # -- 查询与终止 ------------------------------------------------------------ #
    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def kill(self, task_id: str) -> bool:
        """协作终止：在轮次 / 批次边界生效，不强杀线程。"""
        task = self._tasks.get(task_id)
        if task is None or task.cancel is None:
            return False
        if task.status not in ("运行中",):
            return False
        task.cancel.cancel()
        task.status = "已终止"
        task.finished_at = datetime.now().strftime("%H:%M:%S")
        return True


class SubagentManager:
    """调度入口：角色解析 → 模式选择（定义式 / Fork）→ 提交执行。"""

    def __init__(self, roles: list, task_manager: TaskManager, notice, worktree_manager=None, team_coordinator=None) -> None:
        self._roles: dict[str, object] = {r.name: r for r in roles}
        self.tasks = task_manager
        self._notice = notice
        self._runtime = None
        self._worktrees = worktree_manager
        self._team = team_coordinator

    @property
    def role_names(self) -> list[str]:
        return list(self._roles)

    def bind(self, runtime) -> None:
        self._runtime = runtime
        self.tasks.bind(runtime)

    def start_task(self, role_name: str | None, task: str, background: bool) -> str:
        """Agent 工具入口。返回文本作为工具结果回写主对话。"""
        if self._runtime is None:
            return "错误：子工作者系统未就绪。"
        runtime = self._runtime
        # v14 团队桥接：小组存在且 role 是成员 → 走成员派发（协作工具 + 簿记）
        if (
            role_name
            and self._team is not None
            and self._team.team is not None
            and role_name in self._team.team.members
        ):
            return self._team.start_member_task(role_name, task)

        if role_name:
            role = self._roles.get(role_name)
            if role is None:
                return f"未知角色：{role_name}。可用：{', '.join(self._roles) or '（无）'}"

            def runner_defined(token):
                from .runner import run_defined

                return run_defined(
                    role, task, runtime.config, runtime.registry,
                    runtime._ask, runtime._ui, hooks=runtime.hooks,
                    background=background,
                    worktree_manager=self._worktrees,
                )

            task_id, went_background = self.tasks.submit(role_name, runner_defined, background)
            if not went_background:
                return self.tasks.get(task_id).result
            return f"子工作者（角色 {role_name}）已转后台，任务 {task_id}；完成后自动通知，/tasks {task_id} 查看详情。"

        # Fork 式：继承父历史 + 父注册表，强制后台
        def runner_fork(token):
            from .runner import run_fork

            return run_fork(
                task, runtime.config, runtime.registry,
                runtime.session.messages(), runtime.instructions, runtime.skills,
                runtime._ask, runtime._ui, hooks=runtime.hooks, background=True,
            )

        task_id, _ = self.tasks.submit("fork", runner_fork, background=True)
        return f"Fork 子工作者已转后台（任务 {task_id}）；完成后自动通知，/tasks {task_id} 查看详情。"
