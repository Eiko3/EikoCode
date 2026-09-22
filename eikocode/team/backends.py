"""成员运行后端（v14）：window（独立窗格 CLI）与 inline（同进程线程）。

自动选择按环境能力：窗格可用 → window，否则 inline；选择结果终端明示，
不静默降级（spec.md §3 能力 112）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path


def window_available() -> bool:
    """独立窗格能力探测：Windows Terminal 会话或 start 命令可用。"""
    if os.environ.get("WT_SESSION"):
        return True
    return sys.platform == "win32"  # Windows 上 cmd start 总可用


def select_backend(preferred: str = "") -> str:
    """按环境自动选择后端；显式指定且合法时优先。"""
    if preferred in ("window", "inline"):
        return preferred
    return "window" if window_available() else "inline"


def spawn_window_member(team_name: str, member_name: str, python_exe: str, repo_dir: Path) -> bool:
    """window 后端：在独立终端窗格派生一个成员模式的 CLI 实例。"""
    args = [
        "cmd", "/c", "start", f"Member:{member_name}",
        "/min", python_exe, "-m", "eikocode",
        "--team", team_name, "--as", member_name,
    ]
    try:
        subprocess.Popen(args, cwd=str(repo_dir))
        return True
    except OSError:
        return False


def run_member_loop(team_name: str, member_name: str, config, base_dir: Path) -> int:
    """成员模式主循环（window 后端的 CLI 入口）：轮询邮箱 → 处理 → 回报。

    独立进程内以 --team / --as 启动：每收到一条消息按任务执行一轮
    工具循环，结果经 lifecycle 消息回报 Lead。
    """
    from ..config import load_config
    from ..session import Session
    from ..tools import ToolRegistry
    from ..subagent.loader import discover_roles
    from ..subagent.runner import build_filtered_registry
    from ..agent import AgentRuntime, CancelToken
    from ..agent.events import EventKind
    from ..agent.loop import _Ui
    from .models import Team
    from .tools import build_collab_tools

    cfg = load_config(Path.cwd())
    team = Team.load(team_name, base_dir)
    if team is None:
        print(f"小组 {team_name} 不存在")
        return 2
    member = team.members.get(member_name)
    if member is None:
        print(f"小组 {team_name} 中没有成员 {member_name}")
        return 2

    mailbox_dir = team.dir
    from .mailbox import Mailbox

    mailbox = Mailbox(mailbox_dir)
    instance_id = f"{member_name}-{uuid.uuid4().hex[:6]}"
    mailbox.register(member_name, instance_id)

    roles, _ = discover_roles(Path.cwd() / ".eikocode" / "agents",
                              Path.home() / ".eikocode" / "agents")
    role = next((r for r in roles if r.name == member.role), None)
    if role is None:
        print(f"角色 {member.role} 不存在")
        return 2

    registry = build_filtered_registry(ToolRegistry(), role, background=False)
    for tool in build_collab_tools(team, member_name, mailbox):
        registry.register(tool)

    session = Session()
    runtime = AgentRuntime(
        config=cfg, session=session, registry=registry,
        ask=lambda p: "n",  # 成员模式无人在场：确认按拒绝
        ui=_Ui(error=lambda t: print("ERR:", t), notice=lambda t: print("NOTE:", t)),
        model=role.model or cfg.model,
        instructions=f"【小组 {team_name} 成员：{member_name}】角色 {role.description}\n\n{role.sop}",
    )

    print(f"成员 {member_name} 已上线（实例 {instance_id}），等待消息…")
    mailbox.send(to="lead", sender=member_name,
                 text=f"成员 {member_name} 已上线", kind="lifecycle", summary="成员上线")
    idle = False
    while True:
        messages = mailbox.read(instance_id)
        for msg in messages:
            if msg.get("kind") == "shutdown":
                print("收到终止消息，退出")
                return 0
            task_text = str(msg.get("text", ""))
            if not task_text:
                continue
            idle = False
            final = ""
            for event in runtime.run_turn(task_text, CancelToken()):
                if event.kind is EventKind.FINAL_REPLY:
                    final = event.text
            mailbox.send(to="lead", sender=member_name,
                         text=final or "（无输出）", kind="lifecycle",
                         summary=f"任务完成：{(msg.get('summary') or '')[:60]}")
        idle = True
        try:
            import time
            time.sleep(2)
        except KeyboardInterrupt:
            return 0
