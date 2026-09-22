"""命令框架测试（离线，v9）。

覆盖 checklist.md 组 66–73 与 E31：
- 注册中心：字段齐全、冲突检测、隐藏标记
- 解析器：斜杠识别、大小写不敏感、BOM 剥除、参数切分、非命令分流
- 界面控制接口：ctx 方法、命令零渲染器依赖
- 内置命令迁移：13 条行为零变化、别名、隐藏命令
- 预设提示词：/review、/explain
- Tab 补全：唯一匹配、多重匹配、无匹配、候选排除隐藏
- 提示符状态段与未知命令引导
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

import eikocode.cli as cli
from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.loop import _Ui
from eikocode.commands import (
    CommandContext,
    CommandKind,
    CommandRegistry,
    CommandSpec,
    build_builtin_registry,
    complete_command,
    parse,
)
from eikocode.config import Config
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.renderer import Renderer
from eikocode.session import Session
from eikocode.tools import get_registry


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _make_renderer():
    buffer = StringIO()
    console = Console(
        file=buffer, no_color=True, highlight=False, soft_wrap=True, width=100
    )
    return Renderer(False, console), buffer


def _make_config(**overrides) -> Config:
    kwargs = dict(
        model="claude-sonnet-5",
        temperature=0.7,
        max_tokens=4096,
        context_limit=200000,
        known_models=(),
        anthropic_api_key="test-ant",
        openai_api_key="test-oai",
        openai_base_url=None,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_runtime(config):
    return AgentRuntime(
        config=config,
        session=Session(),
        registry=get_registry(),
        ask=lambda p: "y",
        ui=_Ui(error=lambda t: None, notice=lambda t: None),
        model=config.model,
    )


def _ctx(**overrides):
    """构造命令上下文（真渲染器 + 缓冲，便于断言输出）。"""
    renderer, buffer = _make_renderer()
    config = _make_config(**overrides)
    runtime = _make_runtime(config)
    ctx = CommandContext(
        session=runtime.session,
        runtime=runtime,
        config=config,
        cancel=CancelToken(),
        renderer=renderer,
        mcp=None,
        notes=None,
        command_registry=build_builtin_registry(),
    )
    return ctx, buffer, config


def _run(registry, ctx, line: str):
    parsed = parse(line)
    spec = registry.get(parsed.name)
    assert spec is not None, line
    return spec.handler(ctx, parsed.arg)


# --------------------------------------------------------------------------- #
# 组 66：注册中心与解析
# --------------------------------------------------------------------------- #
def test_builtin_commands_registered():
    registry = build_builtin_registry()
    visible = {spec.name for spec in registry.visible()}
    assert visible == {
        "/help", "/model", "/usage", "/clear", "/compact", "/plan", "/review",
        "/explain", "/cancel", "/auto", "/mode", "/mcp", "/sessions",
        "/resume", "/notes", "/skills", "/tasks", "/worktree", "/team", "/exit",
    }


def test_register_conflict_raises():
    registry = CommandRegistry()
    spec = CommandSpec(
        name="/x", description="", usage="/x", kind=CommandKind.LOCAL,
        handler=lambda ctx, arg: None,
    )
    registry.register(spec)
    with pytest.raises(EikoCodeError):
        registry.register(spec)  # 重名
    with pytest.raises(EikoCodeError):
        registry.register(CommandSpec(
            name="/y", description="", usage="/y", kind=CommandKind.LOCAL,
            handler=lambda ctx, arg: None, aliases=("/x",),
        ))  # 别名冲突


def test_spec_fields_readable():
    registry = CommandRegistry()
    spec = CommandSpec(
        name="/x", description="描述", usage="/x <a>",
        kind=CommandKind.PROMPT, handler=lambda ctx, arg: None,
        param_hint="<a>", aliases=("/xx",), hidden=True,
    )
    registry.register(spec)
    got = registry.get("/xx")
    assert got is spec
    assert got.description == "描述" and got.hidden and got.kind == CommandKind.PROMPT


def test_parse_case_insensitive_and_args():
    parsed = parse("/HELP extra stuff")
    assert parsed.is_command and parsed.name == "/help" and parsed.arg == "extra stuff"


def test_parse_strips_bom_and_multiple_spaces():
    parsed = parse("\ufeff/clear")
    assert parsed.is_command and parsed.name == "/clear"
    parsed = parse("/compact   now")
    assert parsed.name == "/compact" and parsed.arg == "now"


def test_parse_non_command_goes_to_model():
    parsed = parse("帮我写个函数")
    assert parsed.is_command is False
    assert parsed.text == "帮我写个函数"


# --------------------------------------------------------------------------- #
# 组 67：界面控制接口
# --------------------------------------------------------------------------- #
def test_ctx_methods_cover_command_dependencies():
    ctx, buffer, config = _ctx()
    ctx.notify("通知")
    ctx.info("信息")
    ctx.error("错误")
    ctx.show_usage()
    out = buffer.getvalue()
    for piece in ("通知", "信息", "错误", "已用", "200,000"):
        assert piece in out


def test_commands_have_no_renderer_dependency():
    """命令实现不 import 渲染器与 CLI 内部符号（grep 证伪，checklist 组 67）。"""
    import pathlib

    src = pathlib.Path("eikocode/commands")
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "from eikocode.renderer" not in text, py
        assert "from eikocode import cli" not in text, py


def test_commands_access_dependencies_via_ctx():
    ctx, _, _ = _ctx()
    assert ctx.session is not None
    assert ctx.runtime is not None
    assert ctx.cancel is not None


# --------------------------------------------------------------------------- #
# 组 68：内置命令迁移（行为零变化）
# --------------------------------------------------------------------------- #
def test_help_lists_every_visible_command():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    _run(registry, ctx, "/help")

    output = buffer.getvalue()
    for spec in registry.visible():
        assert spec.name in output
    assert "/debug" not in output  # 隐藏命令不出现在帮助


def test_exit_command_terminates_with_zero():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    result = _run(registry, ctx, "/exit")
    assert result.exit is True and result.code == 0


def test_exit_alias_quit():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    result = _run(registry, ctx, "/quit")
    assert result.exit is True


def test_clear_alias_reset():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    ctx.session.add_user("提问")
    ctx.session.complete_assistant("回答")
    _run(registry, ctx, "/reset")
    assert ctx.session.turns == 0


def test_model_switch_accepts_known_name():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    result = _run(registry, ctx, "/model gpt-4o")
    assert result.model == "gpt-4o"
    assert "已切换模型：gpt-4o" in buffer.getvalue()


def test_model_switch_rejects_unknown_name():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    result = _run(registry, ctx, "/model nope")
    assert result.model is None
    assert "未知模型：nope" in buffer.getvalue()


def test_plan_toggles_plan_only():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    _run(registry, ctx, "/plan")
    assert ctx.runtime.plan_only is True
    _run(registry, ctx, "/plan off")
    assert ctx.runtime.plan_only is False


def test_auto_toggles_auto_approve():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    _run(registry, ctx, "/auto")
    assert ctx.runtime.auto_approve is True
    _run(registry, ctx, "/auto off")
    assert ctx.runtime.auto_approve is False


def test_auto_on_states_dangerous_still_blocked():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    _run(registry, ctx, "/auto on")
    assert ctx.runtime.auto_approve is True
    assert "工具自动批准已开启" in buffer.getvalue()
    assert "危险命令" in buffer.getvalue()


def test_auto_off_reports_closed():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx(auto_approve=True)
    _run(registry, ctx, "/auto off")
    assert ctx.runtime.auto_approve is False
    assert "工具自动批准已关闭" in buffer.getvalue()


def test_runtime_reads_auto_approve_from_config():
    ctx_on, _, _ = _ctx(auto_approve=True)
    assert ctx_on.runtime.auto_approve is True
    ctx_off, _, _ = _ctx()
    assert ctx_off.runtime.auto_approve is False


def test_mode_switches_three_levels():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    assert ctx.runtime.mode == "default"
    _run(registry, ctx, "/mode strict")
    assert ctx.runtime.mode == "strict"
    assert "权限档位已切换：strict" in buffer.getvalue()
    _run(registry, ctx, "/mode permissive")
    assert ctx.runtime.mode == "permissive"
    assert ctx.runtime.auto_approve is True  # 放行档 = v4 自动批准


def test_mode_without_arg_shows_current():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    _run(registry, ctx, "/mode")
    assert "当前权限档位：default" in buffer.getvalue()


def test_mode_rejects_unknown_level():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    _run(registry, ctx, "/mode yolo")
    assert "未知档位：yolo" in buffer.getvalue()
    assert ctx.runtime.mode == "default"


def test_cancel_command_requests_cancel():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    _run(registry, ctx, "/cancel")
    assert ctx.cancel.is_cancelled() is True


def test_clear_resets_turns():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    ctx.session.add_user("提问")
    ctx.session.complete_assistant("回答")
    _run(registry, ctx, "/clear")
    assert ctx.session.turns == 0


def test_usage_reports_used_limit_and_turns():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    ctx.session.add_user("提问")
    _run(registry, ctx, "/usage")
    output = buffer.getvalue()
    assert "已用" in output
    assert "200,000" in output
    assert "1 轮" in output


def test_debug_hidden_command_works():
    registry = build_builtin_registry()
    ctx, buffer, _ = _ctx()
    result = _run(registry, ctx, "/debug")
    assert result.exit is False
    assert "会话消息数" in buffer.getvalue()


# --------------------------------------------------------------------------- #
# 组 70：预设提示词命令
# --------------------------------------------------------------------------- #
def test_review_prompt_command():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    result = _run(registry, ctx, "/review")
    spec = registry.get("/review")
    assert spec.kind == CommandKind.PROMPT
    assert "审查" in result.prompt_text
    assert "严重" in result.prompt_text  # 按严重度排序


def test_explain_prompt_command():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx()
    result = _run(registry, ctx, "/explain")
    spec = registry.get("/explain")
    assert spec.kind == CommandKind.PROMPT
    assert "解释" in result.prompt_text


# --------------------------------------------------------------------------- #
# 组 71：Tab 补全
# --------------------------------------------------------------------------- #
def test_completion_candidates_exclude_hidden():
    registry = build_builtin_registry()
    candidates = registry.completion_candidates()
    assert "/help" in candidates and "/quit" in candidates
    assert "/debug" not in candidates


def test_completion_unique_match_expands():
    text, candidates = complete_command("/co", ["/compact", "/clear", "/mode"])
    assert text == "/compact "
    assert candidates == []


def test_completion_multiple_matches_lists():
    text, candidates = complete_command("/m", ["/mode", "/mcp", "/help"])
    assert text == "/m"
    assert candidates == ["/mcp", "/mode"]


def test_completion_no_match_keeps_text():
    text, candidates = complete_command("/zzz", ["/mode", "/mcp"])
    assert text == "/zzz" and candidates == []


# --------------------------------------------------------------------------- #
# 组 72：提示符状态段与未知命令引导
# --------------------------------------------------------------------------- #
def test_status_segment_reflects_mode_and_usage():
    registry = build_builtin_registry()
    ctx, _, _ = _ctx(context_limit=1000)
    _run(registry, ctx, "/plan")
    segment = ctx.status_segment()
    assert "计划" in segment and "%" in segment


def test_unknown_command_guide_format():
    guide = cli.UNKNOWN_COMMAND_GUIDE.format(name="/nope")
    assert "/help" in guide
    assert "去掉开头的 /" in guide
