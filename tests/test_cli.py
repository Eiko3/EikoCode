"""CLI 入口层测试（离线）。

v3 起 cli 不再是内联工具循环，而是代理运行时的事件消费者。
命令测试自 v9 起迁移到 tests/test_commands.py（集中式注册中心）。
- 启动不支持工具即报错退出
- 驱动事件流：渲染最终回复入会话、Ctrl+C 干净回滚
"""

from io import StringIO

import pytest
from rich.console import Console

from eikocode import cli
from eikocode import agent
from eikocode.agent import AgentRuntime, CancelToken, EventKind
from eikocode.config import Config
from eikocode.providers.base import GenerateParams, ToolCall
from eikocode.renderer import Renderer
from eikocode.session import Session
from eikocode.tools import get_registry


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
        ui=agent.loop._Ui(error=lambda t: None, notice=lambda t: None),
        model=config.model,
    )


def test_runtime_deny_writes_reason_to_session(monkeypatch):
    """越界写被流水直接拒绝：结果回写带依据、终端可见、底层未执行。"""
    from eikocode.tools.base import PermissionLevel, Tool

    class _CaptureProvider:
        supports_tools = True
        supports_temperature = True

        def __init__(self):
            self.calls = 0

        def stream(self, messages, params, tools=()):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(
                    id="c1",
                    name="EvilWrite",
                    arguments={"path": "C:\\Windows\\system32\\evil.txt"},
                )
            else:
                yield "完成"

    class _OutsideWriteTool(Tool):
        permission = PermissionLevel.WRITE

        def __init__(self):
            self.name = "EvilWrite"
            self.ran = False

        def execute(self, args):
            self.ran = True
            return "written"

    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider", lambda config, model: _CaptureProvider()
    )
    renderer, buffer = _make_renderer()
    registry = get_registry()
    tool = _OutsideWriteTool()
    registry.register(tool)
    runtime = AgentRuntime(
        config=_make_config(),
        session=Session(),
        registry=registry,
        ask=lambda p: "y",
        ui=agent.loop._Ui(
            error=lambda t: buffer.write(t + "\n"),
            notice=lambda t: buffer.write(t + "\n"),
        ),
        model="claude-sonnet-5",
    )
    events = list(runtime.run_turn("写出去", CancelToken()))
    results = [ev for ev in events if ev.kind is EventKind.TOOL_RESULT]

    assert results[0].text.startswith("路径越界")
    assert tool.ran is False  # 底层一次都没跑
    assert "已拦截" in buffer.getvalue()


# --------------------------------------------------------------------------- #
# 启动：不支持工具即报错退出
# --------------------------------------------------------------------------- #
class _NoToolProvider:
    supports_tools = False
    supports_temperature = True

    def stream(self, messages, params, tools=()):
        yield ""


def test_unsupported_model_refuses_at_startup(monkeypatch):
    monkeypatch.setattr(cli, "select_provider", lambda config, model: _NoToolProvider())

    renderer, buffer = _make_renderer()
    code = cli._repl(_make_config(), renderer)

    assert code == 2
    assert "不支持工具调用" in buffer.getvalue()


# --------------------------------------------------------------------------- #
# 驱动事件流
# --------------------------------------------------------------------------- #
class _EchoProvider:
    """简单假供应商：一次吐最终文本。"""

    def __init__(self, text="你好世界"):
        self.text = text
        self.supports_tools = True
        self.supports_temperature = True

    def stream(self, messages, params: GenerateParams, tools=()):
        yield self.text


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider", lambda config, model: provider
    )


def test_drive_renders_reply_and_records_session(monkeypatch):
    _patch_provider(monkeypatch, _EchoProvider("你好世界"))
    renderer, buffer = _make_renderer()
    runtime = _make_runtime(_make_config())
    session = runtime.session

    cli._drive("提问", runtime, CancelToken(), renderer)

    assert session.turns == 1
    assert session.messages()[1].content == "你好世界"


def test_drive_keyboard_interrupt_rolls_back(monkeypatch):
    class _BoomProvider:
        supports_tools = True
        supports_temperature = True

        def stream(self, messages, params, tools=()):
            raise KeyboardInterrupt()

    _patch_provider(monkeypatch, _BoomProvider())
    renderer, _ = _make_renderer()
    runtime = _make_runtime(_make_config())
    session = runtime.session
    session.add_user("之前的轮次")  # 模拟已有上下文

    cli._drive("提问", runtime, CancelToken(), renderer)

    # 中断：本轮（用户消息 + 半截回复）不入库，只保留之前的轮次
    assert session.turns == 1


class _RecordingRenderer:
    """记录渲染调用顺序的假 renderer，断言确认发生在流式窗口之外。"""

    def __init__(self):
        self.log: list[str] = []
        self.streaming = False

    def begin_stream(self):
        self.streaming = True
        self.log.append("begin")

    def end_stream(self):
        self.streaming = False
        self.log.append("end")

    def feed(self, chunk):
        self.log.append(f"feed:{chunk}")

    def notice(self, text):
        self.log.append(f"notice:{text}")

    def error(self, text):
        self.log.append(f"error:{text}")


class _TextThenToolProvider:
    """第一轮：先吐一段文本再请求工具；第二轮：吐最终文本。"""

    supports_tools = True
    supports_temperature = True

    def __init__(self):
        self.calls = 0

    def stream(self, messages, params: GenerateParams, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield "先写文件"
            yield ToolCall(id="c1", name="WriteFile", arguments={})
        else:
            yield "完成"


def test_drive_confirmation_happens_outside_stream_window(monkeypatch):
    """权限确认必须发生在流式渲染之外。

    v3 回归：_drive 曾在整个事件流期间持有 Rich Live，工具确认的 input()
    在 Live 的后台重绘下执行——提示文字被控制序列清掉、用户输入的回显被
    重绘吞掉，表现为「卡住、按 y 无效」。现在文本段与工具段分开渲染。
    """
    _patch_provider(monkeypatch, _TextThenToolProvider())
    renderer = _RecordingRenderer()
    ask_states: list[bool] = []

    def _ask(prompt: str) -> str:
        ask_states.append(renderer.streaming)
        return "y"

    runtime = AgentRuntime(
        config=_make_config(),
        session=Session(),
        registry=get_registry(),
        ask=_ask,
        ui=agent.loop._Ui(error=renderer.error, notice=renderer.notice),
        model="claude-sonnet-5",
    )
    cli._drive("提问", runtime, CancelToken(), renderer)

    assert ask_states == [False]
    assert renderer.log.index("end") < renderer.log.index("notice:调用工具：WriteFile")
    assert renderer.log.count("begin") == 2
    assert renderer.log[-1] == "end"


def test_drive_never_opens_stream_without_text(monkeypatch):
    """整轮没有任何文本增量时（纯工具轮被取消等），不开启也不残留流式窗口。"""

    class _ToolOnlyProvider:
        supports_tools = True
        supports_temperature = True

        def stream(self, messages, params, tools=()):
            yield ToolCall(id="c1", name="ReadFile", arguments={})

    _patch_provider(monkeypatch, _ToolOnlyProvider())
    renderer = _RecordingRenderer()

    runtime = AgentRuntime(
        config=_make_config(),
        session=Session(),
        registry=get_registry(),
        ask=lambda p: "y",
        ui=agent.loop._Ui(error=renderer.error, notice=renderer.notice),
        model="claude-sonnet-5",
    )
    cli._drive("提问", runtime, CancelToken(), renderer)

    assert "begin" not in renderer.log
    assert "end" not in renderer.log
    assert renderer.streaming is False
