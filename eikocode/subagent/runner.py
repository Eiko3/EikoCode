"""子工作者执行器（v12）：工具过滤防线 + 定义式 / Fork 双模式 + 跑到底。

- 防线（spec.md §3 能力 98）：全局排除启动工具 → 角色黑名单 → 角色白名单
  → 后台再 ∩ 只读集合。
- 定义式：空白会话 + 角色 SOP；Fork：继承父历史 + 父工具集 + 覆盖性指令。
- 跑到底：任务注入后不再请求工具即完成，最后文本为结果。
"""

from __future__ import annotations

import time

from ..agent import AgentRuntime, CancelToken
from ..agent.events import EventKind
from ..agent.loop import _Ui
from ..session import Session
from ..tools import ToolRegistry
from ..tools.base import PermissionLevel
from .models import DEFAULT_MAX_TURNS, READONLY_TOOLS, RoleSpec

AGENT_TOOL_NAME = "Agent"

# Fork 覆盖性指令（提议默认文案，见 checklist 组 92）
FORK_OVERRIDE_INSTRUCTIONS = (
    "【子工作者模式（Fork）】你现在以独立子工作者身份执行任务，必须遵守：\n"
    "1. 不得再调用启动子工作者的工具（Agent）——绝对禁止嵌套。\n"
    "2. 不主动对话、不寒暄、不请求确认，直接用工具完成任务。\n"
    "3. 遇到权限确认会按拒绝处理，改用其他可行路径并在报告中说明。\n"
    "4. 最终报告按结构化字段输出（结果 / 变更文件 / 后续步骤），控制在 300 字以内。\n"
)


def build_filtered_registry(
    base_registry,
    role: RoleSpec | None,
    background: bool,
) -> ToolRegistry:
    """按三层防线构建子工作者注册表。

    第一层：全局排除启动工具自身（任何模式、任何角色，防嵌套失控）；
    第二层：角色黑名单 + 白名单；第三层：后台 ∩ 只读集合。
    系统级 load_skill 保留（供角色引用 Skill），后台模式下随只读过滤移除。
    """
    sub = ToolRegistry()
    for tool in base_registry.all():
        name = tool.name
        if name == AGENT_TOOL_NAME:
            continue  # 第一层：防嵌套
        if role is not None and name in role.deny_tools:
            continue  # 第二层：角色黑名单
        if role is not None and role.tools and name not in role.tools:
            continue  # 第二层：角色白名单
        if background and (
            name not in READONLY_TOOLS or tool.permission is not PermissionLevel.READ
        ):
            continue  # 第三层：后台只读
        sub.register(tool)
    return sub


def run_defined(
    role: RoleSpec,
    task: str,
    config,
    base_registry,
    ask,
    ui: _Ui,
    hooks=None,
    background: bool = False,
    worktree_manager=None,
) -> str:
    """定义式执行：空白会话 + 角色 SOP 系统提示 + 任务注入，跑到底。

    角色声明 `worktree: true` 时自动创建并进入工作树，任务前注入路径
    说明；完成后按变更自动判断保留（有变更）或清理（无变更）。
    """
    sub_registry = build_filtered_registry(base_registry, role, background)
    sub_session = Session()

    wt_note = ""
    created_wt: str | None = None
    if role.worktree and worktree_manager is not None:
        wt_name = f"subagent-{int(time.time())}"
        created = worktree_manager.create(wt_name, getattr(config, "_raw", None))
        if "已创建" in created or "已存在" in created:
            created_wt = wt_name
            wt_path = worktree_manager.worktree_path(wt_name)
            wt_note = f"【工作树隔离】所有文件操作请使用目录：{wt_path}\n\n"
        else:
            ui.notice(f"工作树创建失败，子任务将在主目录执行：{created}")

    def _ask_background(prompt: str) -> str:
        # 后台无人可确认：一律拒绝并记录（spec.md §3 能力 99）
        ui.notice(f"后台任务权限确认按拒绝处理：{prompt[:80]}")
        return "n"

    sub_runtime = AgentRuntime(
        config=config,
        session=sub_session,
        registry=sub_registry,
        ask=_ask_background if background else ask,  # 前台确认真实到达用户
        ui=ui,
        model=role.model or config.model,
        instructions=f"【角色：{role.name}】{role.description}\n\n{role.sop}",
        hooks=hooks,  # Hook 引擎共享生效
    )
    try:
        return _run_to_completion(sub_runtime, f"{wt_note}【任务】\n{task}", role.max_turns)
    finally:
        if created_wt and worktree_manager is not None:
            wt_path = worktree_manager.worktree_path(created_wt)
            # 保留 / 清理只看工作区是否脏（未提交修改）；分支本身未推送不算——
            # 子工作者按设计不推送，以分支保留成果，目录清理交给过期清理器
            if worktree_manager.has_uncommitted(wt_path):
                ui.notice(f"子任务工作树 {created_wt} 有未提交变更，已保留（/worktree list 查看）")
            else:
                worktree_manager.delete(created_wt, force=True)
                ui.notice(f"子任务工作树 {created_wt} 无变更，已清理")


def run_fork(
    task: str,
    config,
    base_registry,
    parent_messages: tuple,
    parent_instructions: str,
    parent_skills,
    ask,
    ui: _Ui,
    hooks=None,
    max_turns: int = DEFAULT_MAX_TURNS,
    background: bool = True,
) -> str:
    """Fork 式执行：继承父历史 + 父工具集（过滤后）+ 覆盖性指令，强制后台。"""
    sub_registry = build_filtered_registry(base_registry, None, background)
    sub_session = Session()
    sub_session.rewrite(list(parent_messages))

    def _ask_background(prompt: str) -> str:
        ui.notice(f"后台任务权限确认按拒绝处理：{prompt[:80]}")
        return "n"

    sub_runtime = AgentRuntime(
        config=config,
        session=sub_session,
        registry=sub_registry,
        ask=_ask_background if background else ask,
        ui=ui,
        model=config.model,
        instructions=parent_instructions,  # 与父一致 → 稳定前缀逐字节一致（缓存命中）
        hooks=hooks,
    )
    # 复用父的 Skill 装配状态，保证 pinned 段与父请求一致
    sub_runtime.skills = parent_skills

    prompt = f"{FORK_OVERRIDE_INSTRUCTIONS}\n【任务】\n{task}"
    return _run_to_completion(sub_runtime, prompt, max_turns)


def _run_to_completion(runtime: AgentRuntime, prompt: str, max_turns: int) -> str:
    """跑到底：收集最终回复；超轮数 / 出错视为失败并返回说明文本。"""
    runtime._max_turns_override = max_turns
    final_text = ""
    error_text = ""
    for event in runtime.run_turn(prompt, CancelToken()):
        if event.kind is EventKind.FINAL_REPLY:
            final_text = event.text
        elif event.kind is EventKind.ERROR:
            error_text = event.text
    if error_text and not final_text:
        return f"子工作者执行失败：{error_text}"
    return final_text or "子工作者执行完成（无文本输出）。"
