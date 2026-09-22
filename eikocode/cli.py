"""入口层：输入循环、分流器与渲染。

v3 起，主循环不再内联驱动工具循环——它构造 `AgentRuntime` 并订阅其事件流，
逐类渲染。循环逻辑、批处理、plan-only、取消全部在 `eikocode/agent/` 里。
v9 起，斜杠命令经 `eikocode/commands/` 的解析器分流到集中注册中心，本模块
不再内联实现任何命令。

事件流的渲染是入口层唯一的责任；循环过程本身不在这里。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .agent import AgentRuntime, CancelToken, EventKind
from .agent.loop import _Ui
from .commands import (
    CommandContext,
    build_builtin_registry,
    complete_command,
    install_readline_completion,
    parse,
)
from .config import Config, load_config
from .errors import (
    INTERRUPT_MESSAGE,
    ErrorKind,
    EikoCodeError,
    describe_unexpected,
    exit_code_for,
    message_for,
)
from .memory import NotesManager, SessionArchiver, load_instructions
from .memory.archive import list_archives, load_archive, stale_days
from .hooks import HookEngine, load_hooks
from .worktree import WorktreeManager, cleanup_expired
from .team import TeamCoordinator
from .team.backends import run_member_loop
from .skills import SkillManager
from .subagent import AgentTool, SubagentManager, TaskManager, discover_roles
from .mcp.manager import McpManager
from .providers import select_provider
from .renderer import Renderer
from .session import Session
from .terminal import DEGRADED_NOTICE, detect_color_support, ensure_utf8
from .tools import get_registry

# 品牌名的唯一来源。Python 包名（eikocode/ 目录、pyproject 的 name）按 PEP 8 必须小写，
# 跟这里的用户可见品牌名是两回事，不要混为一谈。
APP_NAME = "EikoCode"

CLEARED_MESSAGE = "会话已清空。"

# 自动批准开启时的启动提示。只在这一次说清楚边界，之后不再逐条打扰。
AUTO_APPROVE_NOTICE = (
    "工具自动批准已开启：读 / 写 / 非危险 Shell 直接执行，不再逐条确认；"
    "命中危险命令仍会拦下要求输入 yes。用 /auto off 可关闭。"
)

UNKNOWN_COMMAND_GUIDE = (
    "未知命令：{name}。输入 /help 查看全部命令；"
    "要向 AI 提问，请去掉开头的 / 直接输入。"
)

# 工具结果渲染时截断到该长度，避免长结果刷屏（事件本身仍携带完整文本）。
_TOOL_RESULT_PREVIEW = 300


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    ensure_utf8()
    resume = "--resume" in args
    args = [a for a in args if a != "--resume"]
    # v14：成员模式（window 后端派生的独立实例）
    if "--team" in args and "--as" in args:
        i = args.index("--team")
        j = args.index("--as")
        if i + 1 < len(args) and j + 1 < len(args):
            ensure_utf8()
            from .config import load_config as _lc

            cfg = _lc()
            if not cfg.has_any_credential:
                print(message_for(ErrorKind.MISSING_CREDENTIAL))
                return exit_code_for(ErrorKind.MISSING_CREDENTIAL)
            return run_member_loop(args[i + 1], args[j + 1], cfg, Path.cwd())

    if "--version" in args:
        print(f"{APP_NAME} {__version__}")
        return 0

    color = detect_color_support()
    renderer = Renderer(color)
    if not color:
        renderer.notice(DEGRADED_NOTICE)

    try:
        config = load_config()
    except EikoCodeError as exc:
        renderer.error(exc.user_message)
        return exc.exit_code

    if not config.has_any_credential:
        renderer.error(message_for(ErrorKind.MISSING_CREDENTIAL))
        return exit_code_for(ErrorKind.MISSING_CREDENTIAL)

    return _repl(config, renderer, resume=resume)


def _build_prompt(ctx: CommandContext) -> str:
    """提示符内嵌状态段（v9）：当前模式 + 用量占比。

    不做常驻底栏——Rich Live 的常驻渲染会与流式输出冲突（spec.md §6）。
    """
    return f"{APP_NAME} [{ctx.status_segment()}]> "


def _repl(config: Config, renderer: Renderer, resume: bool = False) -> int:
    session = Session()
    model = config.model
    renderer.banner([f"{APP_NAME} v{__version__}", f"模型：{model}"])
    _note_sampling_support(config, model, renderer)

    # 启动即确认当前模型支持工具调用；不支持则明确报错退出，不静默降级到文本解析。
    try:
        provider = select_provider(config, model)
    except EikoCodeError as exc:
        renderer.error(exc.user_message)
        return exc.exit_code
    if not provider.supports_tools:
        renderer.error(
            "当前模型不支持工具调用，无法启动带工具的模式。"
            "请改用支持工具调用的模型（如 claude-sonnet-5 或 deepseek-chat）。"
        )
        return 2

    tool_registry = get_registry()
    cancel = CancelToken()

    # v8：项目指令（会话内固定，注入对话最早位置）
    instructions, loaded_files = load_instructions(Path.cwd(), Path.home() / ".eikocode")
    for desc in loaded_files:
        renderer.notice(f"已加载项目指令：{desc}")

    # v8：会话存档与自动笔记
    archiver = SessionArchiver()
    renderer.notice(f"会话存档已开启（{archiver.session_id}）")
    notes = NotesManager(config, renderer.notice)

    runtime = AgentRuntime(
        config=config,
        session=session,
        registry=tool_registry,
        ask=lambda p: input(p),
        ui=_Ui(error=renderer.error, notice=renderer.notice),
        model=model,
        instructions=instructions,
        archiver=archiver,
        notes=notes,
    )
    if runtime.auto_approve:
        renderer.notice(AUTO_APPROVE_NOTICE)

    # v6：连接外部工具服务并注册（失败明确指出服务名，其余能力照常）
    mcp = McpManager(config, tool_registry, renderer.notice)
    if config.mcp_servers:
        mcp.connect_all()

    # v12：子工作者系统（角色三级加载 + Agent 工具 + 后台任务）
    task_manager = TaskManager(renderer.notice)
    role_specs, role_errors = discover_roles(
        Path.cwd() / ".eikocode" / "agents",
        Path.home() / ".eikocode" / "agents",
        verify_enabled=bool(getattr(config, "verify_role", False)),
    )
    for err in role_errors:
        renderer.notice(f"角色加载失败：{err}")

    # v13：Git 工作树隔离 + 过期清理（启动时一次）+ --resume 的工作树恢复
    worktrees = WorktreeManager(Path.cwd(), renderer.notice)
    if worktrees.is_repo():
        cleaned = cleanup_expired(worktrees)
        if cleaned:
            renderer.notice(f"已清理过期工作树：{', '.join(cleaned)}")
        if resume:
            restored_wt = worktrees.restore_state(runtime)
            if restored_wt:
                renderer.notice(f"已恢复工作树：{restored_wt}")
    else:
        worktrees = None

    # v14：小组协作（复用角色清单与工作树管理器）
    team_coord = TeamCoordinator(
        Path.cwd(), renderer.notice, config, lambda p: input(p), _Ui(
            error=renderer.error, notice=renderer.notice,
        ),
        roles=role_specs, hooks=None, worktree_manager=worktrees,
    )
    subagents = SubagentManager(role_specs, task_manager, renderer.notice,
                                worktree_manager=worktrees, team_coordinator=team_coord)
    subagents.bind(runtime)
    tool_registry.register(AgentTool(subagents.start_task))
    if role_specs:
        renderer.notice(f"已加载 {len(role_specs)} 个子工作者角色（Agent 工具可用）")

    # v9：命令注册中心（先于 Skill：激活的短命令要注册进来）
    command_registry = build_builtin_registry()

    # v10：Skill 系统（MCP 之后加载，fail-fast 校验含外部工具名）
    skills = SkillManager.load(
        tool_registry,
        renderer.notice,
        command_registry=command_registry,
        project_dir=Path.cwd() / ".eikocode" / "skills",
        user_dir=Path.home() / ".eikocode" / "skills",
    )
    skills.bind(runtime)
    team_coord.bind_base_registry(tool_registry)

    # v11：Hook 系统（用户级 + 项目级都执行；错误逐条定位，不阻断）
    hook_specs, hook_errors = load_hooks(
        project_path=Path.cwd() / ".eikocode" / "hooks.yaml",
        user_path=Path.home() / ".eikocode" / "hooks.yaml",
    )
    for err in hook_errors:
        renderer.notice(f"Hook 配置错误：{err}")
    if hook_specs:
        renderer.notice(f"已加载 {len(hook_specs)} 条 Hook 规则（/hooks.yaml 声明）")
    hooks = HookEngine(hook_specs, renderer.notice, cwd=str(Path.cwd()))
    runtime.hooks = hooks
    team_coord._hooks = hooks  # Hook 引擎在 v12/v14 段之后定型，此处回填共享
    hooks.observe("startup")

    if resume and not session.turns and session.message_count == 0:
        # --resume：恢复最近活跃会话（复用 v8 存档）
        metas = list_archives()
        if metas:
            latest = metas[0]
            try:
                messages, meta = load_archive(str(latest.get("id")))
                stale = stale_days(meta)
                runtime.restore_session(
                    messages,
                    stale_notice=f"（--resume：已恢复会话 {latest.get('id')}）" + (
                        f"距上次对话 {stale} 天。" if stale else ""
                    ),
                )
                renderer.notice(f"已恢复会话 {latest.get('id')}（{len(messages)} 条消息）")
            except EikoCodeError as exc:
                renderer.error(f"恢复会话失败：{exc.user_message}")

    ctx = CommandContext(
        session=session,
        runtime=runtime,
        config=config,
        cancel=cancel,
        renderer=renderer,
        mcp=mcp,
        notes=notes,
        command_registry=command_registry,
        skills=skills,
        tasks=task_manager,
        worktrees=worktrees,
        team=team_coord,
    )
    install_readline_completion(command_registry.completion_candidates)

    try:
        while True:
            try:
                line = input(_build_prompt(ctx))
            except EOFError:
                return 0
            except KeyboardInterrupt:
                renderer.info("")
                continue

            parsed = parse(line)
            if not parsed.is_command:
                if not parsed.text:
                    continue
                code = _drive(parsed.text, runtime, cancel, renderer)
                if code is not None:
                    return code
                cancel = CancelToken()
                continue

            # 命令路径：经注册中心分发
            spec = command_registry.get(parsed.name)
            if spec is None:
                renderer.error(UNKNOWN_COMMAND_GUIDE.format(name=parsed.name))
                continue
            result = spec.handler(ctx, parsed.arg)
            if result.exit:
                return result.code
            if result.prompt_text:
                # 预设提示词送对话流：与普通用户消息同一路径
                code = _drive(result.prompt_text, runtime, cancel, renderer)
                if code is not None:
                    return code
                cancel = CancelToken()
                continue
            if result.model:
                model = result.model
                runtime.set_model(model)
                _note_sampling_support(config, model, renderer)
    except EikoCodeError as exc:
        renderer.error(exc.user_message)
        return exc.exit_code
    except KeyboardInterrupt:
        renderer.notice(INTERRUPT_MESSAGE)
        return 0
    except Exception as exc:  # 兜底：绝不把堆栈甩给用户
        renderer.error(describe_unexpected(exc))
        return 1
    finally:
        try:
            notes.update_now(session)  # 退出时同步更新一次笔记
        finally:
            hooks.shutdown()  # v11：shutdown 事件
            archiver.close()
            mcp.close()  # 回收外部服务子进程与连接


def _drive(text: str, runtime: AgentRuntime, cancel: CancelToken, renderer: Renderer) -> int | None:
    """消费代理运行时的事件流，逐类渲染。

    流式渲染（Rich Live）与交互确认不能同时进行：Live 在后台线程反复重绘，
    会吞掉权限确认的提示文字与用户输入（v3 回归——v3 把工具执行移进了事件流
    内部，而 v2 的确认发生在流式窗口之外）。因此把「文本段」与「工具段」分开：
    第一段文本增量到达时才开流；工具调用开始前先落地文本、停掉流式，权限
    确认永远出现在干净的行上；工具结果渲染完，下一段文本增量再重新开流。

    Ctrl+C 已在运行时内部映射为取消并整轮回滚，这里只兜底处理渲染期的异常。
    """
    streaming = False
    try:
        for ev in runtime.run_turn(text, cancel):
            if ev.kind is EventKind.TEXT_DELTA and not streaming:
                renderer.begin_stream()
                streaming = True
            elif ev.kind is EventKind.TOOL_CALL_START and streaming:
                renderer.end_stream()
                streaming = False
            _render_event(ev, renderer)
    except KeyboardInterrupt:
        cancel.cancel()
    finally:
        if streaming:
            renderer.end_stream()
    return None


def _render_event(ev, renderer: Renderer) -> None:
    """把一条事件渲染到终端。"""
    if ev.kind is EventKind.USER_MESSAGE:
        return  # 用户已自行输入，无需回显
    if ev.kind is EventKind.TEXT_DELTA:
        renderer.feed(ev.text)
    elif ev.kind is EventKind.THINKING:
        renderer.notice(f"（思考）{ev.text}")
    elif ev.kind is EventKind.TOOL_CALL_START:
        renderer.notice(f"调用工具：{ev.tool_name}")
    elif ev.kind is EventKind.TOOL_RESULT:
        preview = ev.text
        if len(preview) > _TOOL_RESULT_PREVIEW:
            preview = preview[:_TOOL_RESULT_PREVIEW] + "…（已截断显示，完整结果已入上下文）"
        renderer.notice(f"{ev.tool_name} → {preview}")
    elif ev.kind is EventKind.FINAL_REPLY:
        return  # 文本已通过 TEXT_DELTA 流式渲染
    elif ev.kind is EventKind.ERROR:
        renderer.error(ev.text)


def _note_sampling_support(config: Config, model: str, renderer: Renderer) -> None:
    """采样参数被供应商忽略时明说，不闷声吞掉用户的配置。"""
    try:
        provider = select_provider(config, model)
    except EikoCodeError:
        return
    if provider.supports_temperature:
        return
    renderer.notice(
        f"当前供应商不接受采样参数，配置中的 temperature={config.temperature} 未生效。"
    )
