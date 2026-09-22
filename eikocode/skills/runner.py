"""隔离模式执行器（v10）：在独立会话中跑一次完整工具循环。

独立会话 = 新 Session + 按白名单构建的新注册表 + 可指定的模型；
主会话历史、已激活 Skill 列表与白名单都不受影响。上下文携带三档
（full = 主对话模型摘要；recent = 最近 10 条原文；none = 不携带）。
最终回复作为工具结果回流主对话（由 SkillManager 交回 load_skill 结果）。
"""

from __future__ import annotations

from ..agent import AgentRuntime, CancelToken
from ..agent.events import EventKind
from ..agent.loop import _Ui
from ..context.summarizer import summarize
from ..providers import select_provider
from ..session import Session
from ..tools import ToolRegistry
from .models import CONTEXT_FULL, CONTEXT_NONE, CONTEXT_RECENT, SkillSpec

# recent 档携带的最近消息条数（提议默认，见 checklist 组 78）
RECENT_COUNT = 10


def _preload_context(
    spec: SkillSpec,
    sub_session: Session,
    main_messages: tuple,
    config,
    notice,
) -> None:
    """按携带档位把主对话内容预置进独立会话。"""
    if spec.context == CONTEXT_NONE or not main_messages:
        return
    if spec.context == CONTEXT_RECENT:
        # 只携带纯文本的用户 / 助手消息：带 tool_calls 的助手消息若被窗口
        # 截断（或其工具结果不在窗口内），请求会被供应商 400 拒绝。
        plain = [m for m in main_messages if m.tool_call_id is None and not m.tool_calls]
        sub_session.rewrite(plain[-RECENT_COUNT:])
        return
    # full：调模型生成主对话摘要（复用 v7 摘要器），独立会话预置
    model = spec.model or config.model
    try:
        provider = select_provider(config, model)
        summary = summarize(provider, model, main_messages)
    except Exception as exc:  # 摘要失败不阻断隔离执行，降级为 none
        notice(f"Skill「{spec.name}」全量摘要生成失败（{exc}），本次不携带主对话上下文")
        return
    sub_session.add_user(f"【主对话摘要（供了解背景）】\n{summary}")


def run_isolated(
    spec: SkillSpec,
    task: str,
    config,
    base_registry: ToolRegistry,
    ask,
    ui: _Ui,
    main_messages: tuple,
    build_sub_registry,
) -> str:
    """以「SOP + 任务」为输入跑一次独立工具循环，返回最终回复文本。

    `build_sub_registry(spec)` 由 SkillManager 提供：按白名单构建独立
    注册表（含目录型工具与系统级 load_skill）。
    """
    sub_session = Session()
    _preload_context(spec, sub_session, main_messages, config, ui.notice)
    sub_registry = build_sub_registry(spec)

    sub_runtime = AgentRuntime(
        config=config,
        session=sub_session,
        registry=sub_registry,
        ask=ask,  # 权限确认必须真实到达用户
        ui=ui,
        model=spec.model or config.model,
        # 隔离会话不带主会话的指令 / 存档 / 笔记 / Skill 装配
    )
    prompt = f"【Skill：{spec.name}】以下是你要遵循的标准作业程序。\n\n{spec.sop}\n\n【任务】\n{task or '（未指定，请按 SOP 处理当前上下文中最相关的事项。）'}"
    final_text = ""
    error_text = ""
    for event in sub_runtime.run_turn(prompt, CancelToken()):
        if event.kind is EventKind.FINAL_REPLY:
            final_text = event.text
        elif event.kind is EventKind.ERROR:
            error_text = event.text
    if error_text and not final_text:
        return f"Skill「{spec.name}」隔离执行失败：{error_text}"
    return final_text or f"Skill「{spec.name}」隔离执行完成（无最终回复）。"
