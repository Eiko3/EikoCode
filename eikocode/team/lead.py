"""Lead 流程与纯调度（v14）：指引注入、成员派生、Git 合并回滚、双重锁定。

Lead 即主对话运行时；成员即带协作工具组的子运行时实例（inline 后端），
或主进程派生的独立 CLI 实例（window 后端）。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import uuid
from pathlib import Path

from ..agent import AgentRuntime, CancelToken
from ..agent.events import EventKind
from ..agent.loop import _Ui
from ..session import Session
from ..subagent.loader import discover_roles
from ..subagent.runner import build_filtered_registry
from .backends import select_backend, spawn_window_member
from .mailbox import LEAD_INSTANCE, Mailbox
from .models import MemberSpec, Team
from .tools import build_collab_tools

LEAD_GUIDELINES = (
    "【小组协作 · Lead 指引】你是小组负责人。工作流：\n"
    "1. 把用户目标拆解为若干任务，用 task_create 写入共享清单（有先后顺序的用 depends_on 声明依赖）。\n"
    "2. 用 Agent 工具派生成员执行各自任务（成员在自己的工作目录里干活）。\n"
    "3. 收集成员的完成通知，必要时经 send_message 协调。\n"
    "4. 全部完成后用 /team merge 合并各成员的工作目录，并向用户汇报。"
)

DISPATCH_GUIDELINES = (
    "【纯调度模式】你现在是纯调度者：禁止直接读写文件或执行命令，"
    "一切实际工作通过派发子工作者完成。工作流：理解需求 → 拆解任务 → "
    "派发 → 收集 → 综合。最终由你向用户输出综合结论。"
)

# 纯调度模式下 Lead 被剥夺的工具（文件与执行类；MCP 工具按命名空间识别）
DISPATCH_DENIED_NAMES = {"ReadFile", "Glob", "Grep", "WriteFile", "EditFile", "Shell"}


def _git(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


class TeamCoordinator:
    """小组编排：创建 / 派生 / 成员执行 / 恢复 / 合并 / 纯调度。"""

    def __init__(
        self,
        base_dir: Path,
        notice,
        config: Config,
        ask,
        ui: _Ui,
        roles: list,
        hooks=None,
        worktree_manager=None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self._notice = notice
        self._config = config
        self._ask = ask
        self._ui = ui
        self._roles = {r.name: r for r in roles}
        self._hooks = hooks
        self._worktrees = worktree_manager
        self.team: Team | None = None
        self._members_runtime: dict[str, AgentRuntime] = {}  # inline 常驻实例（恢复不重 spawn）
        self.dispatch_on = False

    # -- 创建与恢复 ------------------------------------------------------------- #
    def create_team(self, name: str, member_specs: list[tuple[str, str]]) -> str:
        team = Team(name=name, dir=self.base_dir / "teams" / name)
        for member_name, role_name in member_specs:
            team.members[member_name] = MemberSpec(
                name=member_name, role=role_name, backend=select_backend(), needs_approval=True,
            )
        team.save()
        mailbox = Mailbox(team.dir)
        mailbox.register("lead", LEAD_INSTANCE)
        self.team = team
        return f"小组 {name} 已创建（{len(team.members)} 名成员）：{team.dir}"

    def restore_team(self, name: str) -> str:
        team = Team.load(name, self.base_dir)
        if team is None:
            return f"小组 {name} 不存在。"
        self.team = team
        mailbox = Mailbox(team.dir)
        mailbox.register("lead", LEAD_INSTANCE)
        return f"小组 {name} 已恢复（{len(team.members)} 名成员）。"

    # -- 纯调度（双重锁定） -------------------------------------------------------- #
    def enter_dispatch(self, runtime) -> str:
        """双重锁定：配置开关 + 会话内显式命令同时满足才剥夺。"""
        if not bool(getattr(self._config, "team_dispatch_only", False)):
            return "纯调度未生效：需在配置中设置 team_dispatch_only = true 后再次执行 /team dispatch。"
        if self.dispatch_on:
            return "纯调度已在生效中。"
        self.dispatch_on = True
        runtime.registry = _DispatchView(runtime.registry)
        runtime.push_injection(DISPATCH_GUIDELINES)
        self._notice("纯调度模式已开启：Lead 的文件与命令工具已被剥夺，工作经派发完成。")
        return "纯调度模式已开启。"

    def exit_dispatch(self, runtime) -> str:
        if not self.dispatch_on:
            return "纯调度未在生效中。"
        self.dispatch_on = False
        runtime.registry = self._base_registry
        self._notice("纯调度模式已关闭，工具集已恢复。")
        return "纯调度模式已关闭。"

    def bind_base_registry(self, registry) -> None:
        self._base_registry = registry

    # -- 成员派发（Agent 工具桥接） ---------------------------------------------- #
    def start_member_task(self, member_name: str, task: str) -> str:
        """成员派发：复用常驻实例跑任务，后台线程，完成自动通知 Lead。"""
        member = self.team.members[member_name]
        mailbox = self._mailbox_for()
        instance_id = self._instance_id(member_name)
        role = self._roles[member.role]
        team = self.team

        instance = self._members_runtime.get(member_name)
        if instance is None:
            sub_registry = build_filtered_registry(self._base_registry, role, background=False)
            for tool in build_collab_tools(team, member_name, mailbox):
                sub_registry.register(tool)
            sub_runtime = AgentRuntime(
                config=self._config,
                session=Session(),
                registry=sub_registry,
                ask=lambda p: "n",
                ui=_Ui(error=lambda t: self._notice(t), notice=lambda t: self._notice(t)),
                model=role.model or self._config.model,
                instructions=f"【小组 {team.name} 成员：{member_name}】\n\n{role.sop}",
                hooks=self._hooks,
            )
            self._members_runtime[member_name] = sub_runtime  # 常驻：恢复不重 spawn
        else:
            sub_runtime = instance

        member.status = "运行中"
        team.save()

        def _worker() -> None:
            final = ""
            for event in sub_runtime.run_turn(task, CancelToken()):
                if event.kind is EventKind.FINAL_REPLY:
                    final = event.text
            member.status = "空闲"
            member.result = final
            team.save()
            self._notice(f"成员 {member_name} 已空闲：{final[:120]}")
            mailbox.send(to="lead", sender=member_name, text=final or "（无输出）",
                         kind="lifecycle", summary=f"{member_name} 已空闲")

        threading.Thread(target=_worker, daemon=True).start()
        return f"成员 {member_name} 已接任务并转后台；完成后自动通知。"

    # -- 派生与执行 -------------------------------------------------------------- #
    def spawn_members(self, runtime, base_registry) -> str:
        """派生全部成员。window 派生窗格实例；inline 线程执行（等待首轮完成）。"""
        if self.team is None:
            return "小组未创建。"
        backend = select_backend()
        self._notice(f"成员运行后端：{backend}")
        lines = []
        for name, member in self.team.members.items():
            if member.backend == "window" or (member.backend != "inline" and backend == "window"):
                if not spawn_window_member(self.team.name, name, sys.executable, self.base_dir):
                    return f"窗格派生失败：成员 {name} 无法启动（不静默降级）。"
                member.status = "运行中"
                lines.append(f"{name}（window 窗格）")
            else:
                self._start_inline_member(name, member, runtime, base_registry)
                lines.append(f"{name}（inline 线程）")
        self.team.save()
        return "成员已派生：\n" + "\n".join(lines)

    def _mailbox_for(self) -> Mailbox:
        return Mailbox(self.team.dir)

    def _instance_id(self, name: str) -> str:
        return Mailbox(self.team.dir).resolve(name) or name

    def _start_inline_member(self, name: str, member: MemberSpec, runtime, base_registry) -> None:
        """inline 成员：常驻实例 + 线程执行邮箱中的下一批任务；完成标记空闲并通知 Lead。"""
        mailbox = self._mailbox_for()
        instance_id = self._instance_id(name)
        role = self._roles[member.role]
        team = self.team

        instance = self._members_runtime.get(name)
        if instance is None:
            sub_registry = build_filtered_registry(base_registry, role, background=False)
            for tool in build_collab_tools(team, name, mailbox):
                sub_registry.register(tool)
            sub_runtime = AgentRuntime(
                config=self._config,
                session=Session(),
                registry=sub_registry,
                ask=lambda p: "n",  # 成员无人可确认
                ui=_Ui(error=lambda t: self._notice(t), notice=lambda t: self._notice(t)),
                model=role.model or self._config.model,
                instructions=f"【小组 {team.name} 成员：{name}】\n\n{role.sop}",
                hooks=self._hooks,
            )
            self._members_runtime[name] = sub_runtime  # 常驻实例：恢复不重 spawn
        else:
            sub_runtime = instance  # 恢复：会话历史连续

        member.status = "运行中"
        team.save()

        def _worker() -> None:
            messages = mailbox.read(instance_id)
            task_text = "\n".join(str(m.get("text", "")) for m in messages) or "（无任务）"
            final = ""
            for event in sub_runtime.run_turn(task_text, CancelToken()):
                if event.kind is EventKind.FINAL_REPLY:
                    final = event.text
            member.status = "空闲"
            member.result = final
            team.save()
            self._notice(f"成员 {name} 已空闲：{final[:120]}")
            mailbox.send(to="lead", sender=name, text=final or "（无输出）",
                         kind="lifecycle", summary=f"{name} 已空闲")

        threading.Thread(target=_worker, daemon=True).start()

    # -- 合并 ------------------------------------------------------------------ #
    def merge_all(self) -> str:
        """全部成员完成后逐分支合并；冲突就地解决不了则回滚上报。"""
        if self.team is None or self._worktrees is None:
            return "小组未创建或工作树系统未启用。"
        report = []
        for name, member in self.team.members.items():
            if not member.worktree:
                continue
            branch = f"worktrees/{member.worktree.replace('/', '-')}"
            ok, out = _git(["merge", branch], cwd=self._worktrees.repo)
            if ok:
                report.append(f"{name}：合并成功")
            else:
                _git(["merge", "--abort"], cwd=self._worktrees.repo)
                report.append(f"{name}：合并冲突，已回滚上报（{out.strip()[:150]}）")
        return "\n".join(report) or "没有需要合并的成员工作目录。"


class _DispatchView:
    """纯调度视图：隐藏文件与执行类工具（含 MCP 命名空间工具）。"""

    def __init__(self, base) -> None:
        self._base = base

    def _allowed(self, name: str) -> bool:
        return name not in DISPATCH_DENIED_NAMES and "__" not in name

    def get(self, name: str):
        if not self._allowed(name):
            return None
        return self._base.get(name)

    def all(self):
        return tuple(t for t in self._base.all() if self._allowed(t.name))

    def names(self):
        return tuple(n for n in self._base.names() if self._allowed(n))

    def register(self, tool):
        self._base.register(tool)

    def unregister(self, name):
        self._base.unregister(name)
