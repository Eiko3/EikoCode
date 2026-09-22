"""内置命令（v9）：13 条既有命令的注册式迁移 + 预设提示词 + 隐藏命令。

行为与迁移前零变化；别名与隐藏标记见 checklist 组 68。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..context import estimate_messages
from ..errors import EikoCodeError
from ..memory import SessionArchiver, list_archives, load_archive
from ..memory.archive import stale_days
from .context import CommandContext
from .registry import CommandKind, CommandRegistry, CommandSpec

CLEARED_MESSAGE = "会话已清空。"

AUTO_APPROVE_TOGGLED = (
    "工具自动批准已{state}：开启时读 / 写 / 非危险 Shell 直接执行不再询问，"
    "命中危险命令仍会拦下要求输入 yes。"
)

# 预设提示词（提议默认文案，见 checklist 组 70）
REVIEW_PROMPT = (
    "请审查本会话中涉及的所有代码改动与文件内容："
    "按问题严重度从高到低排序输出发现的问题（严重 / 一般 / 建议三档），"
    "每条给出位置、问题说明与修复建议；没有问题的方面也请简要确认。"
)

EXPLAIN_PROMPT = (
    "请解释本会话最近涉及的代码：说明其结构、关键逻辑与设计意图，"
    "用简体中文，先给概览再逐块解释。"
)


@dataclass
class CommandResult:
    exit: bool = False
    code: int = 0
    model: str | None = None
    prompt_text: str = ""  # prompt 类命令：要送入对话流的预设文本


# -- 处理函数 --------------------------------------------------------------- #
def _cmd_exit(ctx: CommandContext, arg: str) -> CommandResult:
    return CommandResult(exit=True, code=0)


def _cmd_help(ctx: CommandContext, arg: str) -> CommandResult:
    ctx.show_help(ctx.command_registry.visible())
    return CommandResult()


def _cmd_clear(ctx: CommandContext, arg: str) -> CommandResult:
    ctx.session.clear()
    # v10 联动：清空对话时顺带清空已激活 Skill 并恢复工具集，杜绝旧 SOP 残留
    skills = getattr(ctx, "skills", None)
    if skills is not None and skills.activated:
        skills.deactivate_all()
        ctx.notify("已清空激活的 Skill，工具集已恢复。")
    ctx.notify(CLEARED_MESSAGE)
    return CommandResult()


def _cmd_usage(ctx: CommandContext, arg: str) -> CommandResult:
    ctx.show_usage()
    return CommandResult()


def _cmd_model(ctx: CommandContext, arg: str) -> CommandResult:
    from ..config import is_known_model

    if not arg:
        ctx.info(f"当前模型：{ctx.runtime.model}")
        return CommandResult()
    if not is_known_model(arg, ctx.config.known_models):
        ctx.error(f"未知模型：{arg}")
        return CommandResult()
    ctx.notify(f"已切换模型：{arg}")
    return CommandResult(model=arg)


def _cmd_plan(ctx: CommandContext, arg: str) -> CommandResult:
    if arg == "on":
        ctx.runtime.set_plan_only(True)
    elif arg == "off":
        ctx.runtime.set_plan_only(False)
    else:
        ctx.runtime.set_plan_only(not ctx.runtime.plan_only)
    state = "开启" if ctx.runtime.plan_only else "关闭"
    ctx.notify(f"只规划模式已{state}：开启时写 / 执行类工具被拦截，只跑只读工具产出计划。")
    return CommandResult()


def _cmd_cancel(ctx: CommandContext, arg: str) -> CommandResult:
    ctx.cancel.cancel()
    ctx.notify("已请求取消当前任务；将在下一轮 / 批次边界停止并回滚。")
    return CommandResult()


def _cmd_auto(ctx: CommandContext, arg: str) -> CommandResult:
    if arg == "on":
        ctx.runtime.set_auto_approve(True)
    elif arg == "off":
        ctx.runtime.set_auto_approve(False)
    else:
        ctx.runtime.set_auto_approve(not ctx.runtime.auto_approve)
    state = "开启" if ctx.runtime.auto_approve else "关闭"
    ctx.notify(AUTO_APPROVE_TOGGLED.format(state=state))
    return CommandResult()


def _cmd_mode(ctx: CommandContext, arg: str) -> CommandResult:
    if arg in ("strict", "default", "permissive"):
        ctx.runtime.set_mode(arg)
        label = {
            "strict": "严格（只读也确认）",
            "default": "默认（写 / 执行确认）",
            "permissive": "放行（危险命令仍拦）",
        }[arg]
        ctx.notify(f"权限档位已切换：{arg}（{label}）")
    elif not arg:
        ctx.info(f"当前权限档位：{ctx.runtime.mode}（可用 /mode strict|default|permissive 切换）")
    else:
        ctx.error(f"未知档位：{arg}（可用 strict / default / permissive）")
    return CommandResult()


def _cmd_mcp(ctx: CommandContext, arg: str) -> CommandResult:
    if not arg:
        for line in ctx.mcp.status_lines():
            ctx.info(line)
    elif arg == "reload":
        ctx.mcp.reload()
    else:
        ctx.error("未知子命令：/mcp reload（不带参数查看状态）")
    return CommandResult()


def _cmd_compact(ctx: CommandContext, arg: str) -> CommandResult:
    if arg == "status":
        used = estimate_messages(ctx.session.messages())
        pct = (
            round(used / ctx.config.context_limit * 100, 1)
            if ctx.config.context_limit
            else 100.0
        )
        ctx.info(
            f"上下文 {used:,} tokens（约 {pct}%）· 自动兜底："
            f"{'开' if getattr(ctx.config, 'auto_compact', True) else '关'} · "
            f"熔断：{'是' if ctx.runtime.compact_breaker_open else '否'}"
        )
    else:
        ctx.notify("正在生成摘要…")
        if ctx.runtime.manual_compact():
            ctx.notify("压缩完成：较早对话已替换为结构化摘要（最近 3 轮保留原文）。")
        else:
            ctx.info("无需压缩（历史过短或未达阈值）。")
    return CommandResult()


def _cmd_sessions(ctx: CommandContext, arg: str) -> CommandResult:
    metas = list_archives()
    if not metas:
        ctx.info("暂无历史会话。")
    else:
        for meta in metas[:10]:
            ctx.info(
                f"{meta.get('id', '?')}  {str(meta.get('title', ''))[:30]}  "
                f"{meta.get('message_count', 0)} 条  {meta.get('last_active_at', '')}"
            )
    return CommandResult()


def _cmd_resume(ctx: CommandContext, arg: str) -> CommandResult:
    if not arg:
        ctx.error("用法：/resume <会话id>（/sessions 查看列表）")
        return CommandResult()
    try:
        messages, meta = load_archive(arg)
    except EikoCodeError as exc:
        ctx.error(exc.user_message)
        return CommandResult()
    archiver = SessionArchiver(
        session_id=arg,
        title=str(meta.get("title", "")),
        message_count=len(messages),
        created_at=meta.get("created_at"),
    )
    stale = stale_days(meta)
    stale_notice = (
        f"（提示：距上次对话已 {stale} 天，期间项目可能已发生变化。）" if stale else None
    )
    ctx.runtime.restore_session(messages, archiver=archiver, stale_notice=stale_notice)
    ctx.notify(f"已恢复会话 {arg}（{len(messages)} 条消息）")
    return CommandResult()


def _cmd_notes(ctx: CommandContext, arg: str) -> CommandResult:
    notes = ctx.notes
    if arg.startswith("clear"):
        parts = arg.split()
        scope = parts[1] if len(parts) > 1 else ""
        if scope == "user":
            notes.user_notes_path.write_text("", encoding="utf-8")
            ctx.notify(f"已清空用户级笔记：{notes.user_notes_path}")
        elif scope == "project":
            notes.project_notes_path.write_text("", encoding="utf-8")
            ctx.notify(f"已清空项目级笔记：{notes.project_notes_path}")
        else:
            ctx.error("用法：/notes clear user|project")
        return CommandResult()
    ctx.info(
        f"用户级笔记：{notes.user_notes_path}"
        f"（{'存在' if notes.user_notes_path.is_file() else '尚未创建'}）"
    )
    ctx.info(
        f"项目级笔记：{notes.project_notes_path}"
        f"（{'存在' if notes.project_notes_path.is_file() else '尚未创建'}）"
    )
    ctx.info("提示：每 5 轮对话与退出时自动更新；/notes clear user|project 可清空。")
    return CommandResult()


def _cmd_review(ctx: CommandContext, arg: str) -> CommandResult:
    return CommandResult(prompt_text=REVIEW_PROMPT)


def _cmd_explain(ctx: CommandContext, arg: str) -> CommandResult:
    return CommandResult(prompt_text=EXPLAIN_PROMPT)


def _cmd_debug(ctx: CommandContext, arg: str) -> CommandResult:
    """隐藏命令：输出内部状态，用于排查问题。"""
    ctx.info(f"会话消息数：{ctx.session.message_count}")
    ctx.info(f"轮次：{ctx.session.turns}")
    ctx.info(f"权限档位：{ctx.runtime.mode} · 只规划：{ctx.runtime.plan_only}")
    ctx.info(f"自动批准：{ctx.runtime.auto_approve} · 熔断：{ctx.runtime.compact_breaker_open}")
    return CommandResult()


def _cmd_tasks(ctx: CommandContext, arg: str) -> CommandResult:
    """v12：后台任务管理——列出 / 详情 / 终止。"""
    tasks = getattr(ctx, "tasks", None)
    if tasks is None:
        ctx.info("后台任务系统未启用。")
        return CommandResult()
    parts = arg.split()
    if parts and parts[0] == "kill":
        if len(parts) < 2:
            ctx.error("用法：/tasks kill <任务id>")
            return CommandResult()
        if tasks.kill(parts[1]):
            ctx.notify(f"已请求终止任务 {parts[1]}（在轮次边界生效）。")
        else:
            ctx.error(f"无法终止任务 {parts[1]}（不存在或已结束）。")
        return CommandResult()
    if parts:
        task = tasks.get(parts[0])
        if task is None:
            ctx.error(f"未知任务：{parts[0]}")
            return CommandResult()
        ctx.info(f"任务 {task.id}（{task.role}）· {task.status}")
        ctx.info(f"起 {task.started_at} · 止 {task.finished_at or '（运行中）'} · 约 {task.tokens_estimate:,} tokens")
        ctx.info(f"结果：{task.result[:800] or '（暂无）'}")
        return CommandResult()
    items = tasks.list_tasks()
    if not items:
        ctx.info("当前没有后台任务。让模型调用 Agent 工具并指定 background 即可后台执行。")
        return CommandResult()
    for task in items:
        ctx.info(task.summary_line())
    ctx.info("提示：/tasks <id> 看详情；/tasks kill <id> 终止。")
    return CommandResult()


def _cmd_team(ctx: CommandContext, arg: str) -> CommandResult:
    """v14：小组协作——create / status / dispatch / merge / close。"""
    team = getattr(ctx, "team", None)
    if team is None:
        ctx.error("小组系统未启用。")
        return CommandResult()
    parts = arg.split(maxsplit=1) if arg else []
    sub = parts[0] if parts else "status"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub == "create":
        pieces = rest.split()
        if len(pieces) < 2:
            ctx.error("用法：/team create <组名> [成员名=角色 ...]")
            return CommandResult()
        specs = [(p.split("=", 1)[0], p.split("=", 1)[1]) for p in pieces[1:] if "=" in p]
        ctx.notify(team.create_team(pieces[0], specs))
        return CommandResult()
    if sub == "dispatch":
        if rest == "off":
            ctx.notify(team.exit_dispatch(ctx.runtime))
        else:
            ctx.notify(team.enter_dispatch(ctx.runtime))
        return CommandResult()
    if sub == "merge":
        ctx.notify(team.merge_all())
        return CommandResult()
    if sub == "close":
        if team.team is not None:
            for name, member in team.team.members.items():
                if member.status == "空闲":
                    team._members_runtime.pop(name, None)
            team.team = None
        ctx.notify("小组已关闭（成员实例释放）。")
        return CommandResult()
    # status（缺省）
    if team.team is None:
        ctx.info("当前没有活跃小组。/team create <名> [成员=角色 ...] 创建。")
        return CommandResult()
    team_obj = team.team
    ctx.info(f"小组：{team_obj.name} · Lead：主对话 · 纯调度：{'开' if team.dispatch_on else '关'}")
    for name, member in team_obj.members.items():
        ctx.info(f"  {name}（{member.role}·{member.backend}·{member.status}）")
    ctx.info(team_obj.render_tasks())
    return CommandResult()


def _cmd_worktree(ctx: CommandContext, arg: str) -> CommandResult:
    """v13：Git 工作树管理——create / enter / exit / delete / list / status。"""
    wt = getattr(ctx, "worktrees", None)
    if wt is None:
        ctx.error("工作树系统未启用。")
        return CommandResult()
    parts = arg.split(maxsplit=1) if arg else []
    sub = parts[0] if parts else "status"
    name = parts[1].strip() if len(parts) > 1 else ""
    runtime = ctx.runtime
    if sub == "create":
        if not name:
            ctx.error("用法：/worktree create <名字>")
        else:
            ctx.notify(wt.create(name, ctx.config))
    elif sub == "enter":
        if not name:
            ctx.error("用法：/worktree enter <名字>")
        else:
            ctx.notify(wt.enter(name, runtime))
    elif sub == "exit":
        ctx.notify(wt.exit(runtime))
    elif sub == "delete":
        pieces = name.split()
        force = "force" in pieces[1:]
        target = pieces[0] if pieces else ""
        if not target:
            ctx.error("用法：/worktree delete <名字> [force]")
        else:
            ctx.notify(wt.delete(target, force=force, runtime=runtime))
    elif sub == "list":
        for line in wt.status_lines():
            ctx.info(line)
    elif sub == "status":
        cur = wt.current or "（主目录）"
        ctx.info(f"当前工作树：{cur}")
        for line in wt.status_lines():
            ctx.info(line)
    else:
        ctx.error(f"未知子命令：{sub}（可用 create / enter / exit / delete / list / status）")
    return CommandResult()


def _cmd_skills(ctx: CommandContext, arg: str) -> CommandResult:
    """v10：查看已加载与已激活 Skill；reload 强制重新扫描。"""
    skills = getattr(ctx, "skills", None)
    if skills is None:
        ctx.info("Skill 系统未启用。")
        return CommandResult()
    if arg == "reload":
        skills.reload()
        return CommandResult()
    if arg:
        spec = skills.specs.get(arg)
        if spec is None:
            ctx.error(f"未知 Skill：{arg}")
        else:
            ctx.info(f"名字：{spec.name}")
            ctx.info(f"说明：{spec.description}")
            ctx.info(f"模式：{spec.mode} · 模型：{spec.model or '（当前会话模型）'}")
            ctx.info(f"白名单：{', '.join(spec.tools) if spec.tools else '（不收窄）'}")
            ctx.info(f"来源：{spec.source}")
        return CommandResult()
    for line in skills.status_lines():
        ctx.info(line)
    ctx.info("提示：/skills reload 强制重新扫描（激活列表清空）；/skills <名字> 看详情。")
    return CommandResult()


# -- 注册 ------------------------------------------------------------------- #
def build_builtin_registry() -> CommandRegistry:
    """构造内置命令注册表。"""
    registry = CommandRegistry()
    register = registry.register

    register(CommandSpec(
        name="/help", description="显示帮助", usage="/help",
        kind=CommandKind.LOCAL, handler=_cmd_help,
    ))
    register(CommandSpec(
        name="/model", description="查看或切换模型", usage="/model [模型名]",
        kind=CommandKind.UI, handler=_cmd_model, param_hint="[模型名]",
    ))
    register(CommandSpec(
        name="/usage", description="查看上下文用量", usage="/usage",
        kind=CommandKind.LOCAL, handler=_cmd_usage,
    ))
    register(CommandSpec(
        name="/clear", description="清空当前对话", usage="/clear",
        kind=CommandKind.UI, handler=_cmd_clear, aliases=("/reset",),
    ))
    register(CommandSpec(
        name="/compact", description="手动触发上下文压缩", usage="/compact [status]",
        kind=CommandKind.UI, handler=_cmd_compact, param_hint="[status]",
    ))
    register(CommandSpec(
        name="/plan", description="切换只规划模式", usage="/plan [on|off]",
        kind=CommandKind.UI, handler=_cmd_plan, param_hint="[on|off]",
    ))
    register(CommandSpec(
        name="/review", description="让 AI 审查本会话涉及的代码", usage="/review",
        kind=CommandKind.PROMPT, handler=_cmd_review,
    ))
    register(CommandSpec(
        name="/explain", description="让 AI 解释最近上下文中的代码", usage="/explain",
        kind=CommandKind.PROMPT, handler=_cmd_explain,
    ))
    register(CommandSpec(
        name="/cancel", description="请求取消当前任务", usage="/cancel",
        kind=CommandKind.UI, handler=_cmd_cancel,
    ))
    register(CommandSpec(
        name="/auto", description="切换工具自动批准", usage="/auto [on|off]",
        kind=CommandKind.UI, handler=_cmd_auto, param_hint="[on|off]",
    ))
    register(CommandSpec(
        name="/mode", description="切换权限档位", usage="/mode [strict|default|permissive]",
        kind=CommandKind.UI, handler=_cmd_mode, param_hint="[strict|default|permissive]",
    ))
    register(CommandSpec(
        name="/mcp", description="查看外部工具服务状态", usage="/mcp [reload]",
        kind=CommandKind.LOCAL, handler=_cmd_mcp, param_hint="[reload]",
    ))
    register(CommandSpec(
        name="/sessions", description="列出历史会话", usage="/sessions",
        kind=CommandKind.LOCAL, handler=_cmd_sessions,
    ))
    register(CommandSpec(
        name="/resume", description="恢复历史会话", usage="/resume <会话id>",
        kind=CommandKind.UI, handler=_cmd_resume, param_hint="<会话id>",
    ))
    register(CommandSpec(
        name="/notes", description="查看或清空自动笔记", usage="/notes [clear user|project]",
        kind=CommandKind.LOCAL, handler=_cmd_notes, param_hint="[clear user|project]",
    ))
    register(CommandSpec(
        name="/skills", description="查看已加载 Skill / 强制重新扫描", usage="/skills [reload|名字]",
        kind=CommandKind.LOCAL, handler=_cmd_skills, param_hint="[reload|名字]",
    ))
    register(CommandSpec(
        name="/tasks", description="查看后台子工作者任务", usage="/tasks [id|kill id]",
        kind=CommandKind.LOCAL, handler=_cmd_tasks, param_hint="[id|kill id]",
    ))
    register(CommandSpec(
        name="/team", description="小组协作：创建 / 状态 / 纯调度 / 合并", usage="/team [create|status|dispatch|merge|close ...]",
        kind=CommandKind.LOCAL, handler=_cmd_team, param_hint="[create|status|dispatch|merge|close]",
    ))
    register(CommandSpec(
        name="/worktree", description="管理 Git 工作树隔离目录", usage="/worktree [create|enter|exit|delete|list|status]",
        kind=CommandKind.LOCAL, handler=_cmd_worktree, param_hint="[create|enter|exit|delete|list|status]",
        aliases=("/wt",),
    ))
    register(CommandSpec(
        name="/debug", description="输出内部状态（排查用）", usage="/debug",
        kind=CommandKind.LOCAL, handler=_cmd_debug, hidden=True,
    ))
    register(CommandSpec(
        name="/exit", description="退出", usage="/exit",
        kind=CommandKind.LOCAL, handler=_cmd_exit, aliases=("/quit",),
    ))
    return registry
