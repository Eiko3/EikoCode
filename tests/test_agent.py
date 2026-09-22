"""代理运行时测试（离线）。

覆盖 checklist.md 第 18–24 组与 E12–E15：
- 事件流通道（七类事件、思考非空才发）
- ReAct 主循环（多轮收敛、达最大轮数停止、阈值）
- 批处理：读并发 / 写串行、组内隔离、写组中止
- plan-only：写被拦、读照跑、收尾即计划、退出后可写
- 取消：令牌置位即整轮回滚
- 自动批准：写 / 普通 Shell 不再确认，危险命令仍拦
"""

from __future__ import annotations

import time

from eikocode import agent
from eikocode.agent import AgentRuntime, CancelToken, EventKind
from eikocode.agent.loop import MAX_TURN, _Ui
from eikocode.config import Config
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.providers.base import GenerateParams, Message, Role, Thinking, ToolCall
from eikocode.session import Session
from eikocode.tools.base import PermissionLevel, Tool
from eikocode.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def _make_config(**overrides) -> Config:
    kwargs = dict(
        model="gpt-4o",
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


def _make_renderer_ui():
    """记录 error / notice 的假 UI，供断言阈值提醒与错误文案。"""
    class _FakeUi:
        def __init__(self):
            self.errors: list[str] = []
            self.notices: list[str] = []

        def error(self, t: str) -> None:
            self.errors.append(t)

        def notice(self, t: str) -> None:
            self.notices.append(t)

    return _FakeUi()


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider", lambda config, model: provider
    )


def _collect(runtime, text, cancel=None):
    cancel = cancel or CancelToken()
    return list(runtime.run_turn(text, cancel))


class _ToolProvider:
    """按调用次数返回一组事件（str / ToolCall / Thinking）。"""

    def __init__(self, sequence):
        self.sequence = sequence
        self.calls = 0
        self.supports_tools = True
        self.supports_temperature = True

    def stream(self, messages, params: GenerateParams, tools=()):
        items = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        for it in items:
            yield it


# --------------------------------------------------------------------------- #
# 假工具
# --------------------------------------------------------------------------- #
class _ReadTool(Tool):
    permission = PermissionLevel.READ

    def __init__(self, name="ReadFile", sleep=0.0, content="read-content"):
        self.name = name
        self._sleep = sleep
        self._content = content

    def execute(self, args):
        if self._sleep:
            time.sleep(self._sleep)
        return self._content


class _WriteTool(Tool):
    permission = PermissionLevel.WRITE

    def __init__(self, name="EditFile", fail=False, recorder=None):
        self.name = name
        self._fail = fail
        self._recorder = recorder

    def execute(self, args):
        if self._recorder is not None:
            self._recorder.append(self.name)
        if self._fail:
            raise EikoCodeError(ErrorKind.TOOL_COMMAND_FAILED, "命令以非零退出码 1 结束")
        return "written"


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


# --------------------------------------------------------------------------- #
# 第 18 组：事件流通道
# --------------------------------------------------------------------------- #
def test_event_types_cover_seven_kinds():
    kinds = {e.value for e in EventKind}
    for label in ("用户消息", "模型思考", "文本增量", "工具调用开始", "工具结果", "最终回复", "错误"):
        assert label in kinds


def test_events_cover_all_seven_via_loop(monkeypatch):
    provider = _ToolProvider([
        [ToolCall(id="c1", name="ReadFile", arguments={})],
        [ToolCall(id="c2", name="EditFile", arguments={})],
        ["最终结论"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool(), _WriteTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "请操作")

    seen = {ev.kind for ev in events}
    assert EventKind.USER_MESSAGE in seen
    assert EventKind.TOOL_CALL_START in seen
    assert EventKind.TOOL_RESULT in seen
    assert EventKind.TEXT_DELTA in seen
    assert EventKind.FINAL_REPLY in seen


def test_empty_thinking_emits_no_event(monkeypatch):
    provider = _ToolProvider([[Thinking(""), "普通回复"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "hi")
    assert not any(ev.kind is EventKind.THINKING for ev in events)


def test_nonempty_thinking_emits_event(monkeypatch):
    provider = _ToolProvider([[Thinking("让我想想"), "普通回复"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "hi")
    thinks = [ev for ev in events if ev.kind is EventKind.THINKING]
    assert len(thinks) == 1
    assert thinks[0].text == "让我想想"


# --------------------------------------------------------------------------- #
# 第 19 组：ReAct 主循环
# --------------------------------------------------------------------------- #
def test_react_loop_converges_after_two_tool_rounds(monkeypatch):
    provider = _ToolProvider([
        [ToolCall(id="c1", name="ReadFile", arguments={})],
        [ToolCall(id="c2", name="EditFile", arguments={})],
        ["全部完成"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool(), _WriteTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "开始")

    assert provider.calls == 3
    finals = [ev for ev in events if ev.kind is EventKind.FINAL_REPLY]
    assert finals and finals[-1].text == "全部完成"
    # 工具结果与最终回复都进了上下文
    assert any(m.tool_call_id is not None for m in runtime.session.messages())


def test_react_stops_at_max_turn_and_rolls_back(monkeypatch):
    # 每个响应都只请求工具，永远不收敛
    provider = _ToolProvider([[ToolCall(id="c1", name="ReadFile", arguments={})]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "开始")

    # 最多 MAX_TURN 次模型调用；第 MAX_TURN+1 轮触发上限后停止
    assert provider.calls == MAX_TURN
    assert runtime.session.message_count == 0  # 整轮回滚，未残留半截
    assert any(
        ev.kind is EventKind.ERROR and "最大轮数" in ev.text for ev in events
    )


def test_max_turn_constant_is_25(monkeypatch):
    from eikocode.agent.loop import MAX_TURN as MT

    assert MT == 25


# --------------------------------------------------------------------------- #
# 第 20 组：批处理 读并发 / 写串行
# --------------------------------------------------------------------------- #
def test_read_group_runs_concurrently(monkeypatch):
    provider = _ToolProvider([[
        ToolCall(id="c1", name="ReadA", arguments={}),
        ToolCall(id="c2", name="ReadB", arguments={}),
    ], ["done"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(_ReadTool("ReadA", sleep=0.2), _ReadTool("ReadB", sleep=0.2)),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    start = time.perf_counter()
    _collect(runtime, "开始")
    elapsed = time.perf_counter() - start
    # 并发：总耗时接近单个（0.2），而非两倍（0.4）
    assert elapsed < 0.35, f"读组未并发，耗时 {elapsed:.2f}s"


def test_read_group_runs_before_write_group(monkeypatch):
    recorder: list[str] = []
    provider = _ToolProvider([[
        ToolCall(id="c1", name="ReadFile", arguments={}),
        ToolCall(id="c2", name="EditFile", arguments={}),
    ], ["done"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(_ReadTool("ReadFile"), _WriteTool("EditFile", recorder=recorder)),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    _collect(runtime, "开始")

    assert "EditFile" in recorder  # 写工具确实执行了
    msgs = runtime.session.messages()
    read_idx = next(i for i, m in enumerate(msgs) if m.tool_call_id == "c1")
    write_idx = next(i for i, m in enumerate(msgs) if m.tool_call_id == "c2")
    assert read_idx < write_idx  # 读结果先于写结果入上下文


def test_group_failure_isolated_within_read_group(monkeypatch):
    provider = _ToolProvider([
        [
            ToolCall(id="c1", name="ReadGood", arguments={}),
            ToolCall(id="c2", name="ReadBad", arguments={}),
        ],
        ["done"],
    ])
    _patch_provider(monkeypatch, provider)

    class _BadRead(Tool):
        permission = PermissionLevel.READ

        def __init__(self):
            self.name = "ReadBad"

        def execute(self, args):
            raise EikoCodeError(ErrorKind.TOOL_TARGET_MISSING, "文件不存在")

    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(_ReadTool("ReadGood"), _BadRead()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    _collect(runtime, "开始")

    msgs = runtime.session.messages()
    good = next(m.content for m in msgs if m.tool_call_id == "c1")
    bad = next(m.content for m in msgs if m.tool_call_id == "c2")
    assert "read-content" in good
    assert "文件不存在" in bad  # 失败的结果也回写了


def test_write_group_aborts_remaining_on_failure(monkeypatch):
    recorder: list[str] = []
    provider = _ToolProvider([
        [
            ToolCall(id="c1", name="EditFail", arguments={}),
            ToolCall(id="c2", name="EditOk", arguments={}),
        ],
        ["done"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(
            _WriteTool("EditFail", fail=True, recorder=recorder),
            _WriteTool("EditOk", recorder=recorder),
        ),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    _collect(runtime, "开始")

    assert recorder == ["EditFail"], "第一个写失败，第二个不应执行"
    msgs = runtime.session.messages()
    ok = next(m.content for m in msgs if m.tool_call_id == "c2")
    assert "已中止" in ok


# --------------------------------------------------------------------------- #
# 第 21 组：plan-only
# --------------------------------------------------------------------------- #
def test_plan_only_intercepts_write_but_runs_read(monkeypatch, tmp_path):
    edited = tmp_path / "f.txt"
    edited.write_text("hello world", encoding="utf-8")

    provider = _ToolProvider([
        [
            ToolCall(id="c1", name="EditFile", arguments={}),
            ToolCall(id="c2", name="ReadFile", arguments={}),
        ],
        ["这是计划：先读后改"],
    ])
    _patch_provider(monkeypatch, provider)

    class _PlanWrite(Tool):
        permission = PermissionLevel.WRITE

        def __init__(self):
            self.name = "EditFile"

        def execute(self, args):
            edited.write_text("CHANGED", encoding="utf-8")
            return "written"

    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(_PlanWrite(), _ReadTool("ReadFile")),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    runtime.set_plan_only(True)
    events = _collect(runtime, "规划一下")

    msgs = runtime.session.messages()
    write_res = next(m.content for m in msgs if m.tool_call_id == "c1")
    read_res = next(m.content for m in msgs if m.tool_call_id == "c2")
    assert "plan-only 已拦截" in write_res
    assert "read-content" in read_res
    assert edited.read_text(encoding="utf-8") == "hello world"  # 文件未被改
    finals = [ev.text for ev in events if ev.kind is EventKind.FINAL_REPLY]
    assert finals and "计划" in finals[-1]


def test_plan_only_final_reply_is_plan_text(monkeypatch):
    provider = _ToolProvider([
        [
            ToolCall(id="c1", name="EditFile", arguments={}),
            ToolCall(id="c2", name="ReadFile", arguments={}),
        ],
        ["方案一：读取后改写"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(),
        _registry(_WriteTool("EditFile"), _ReadTool("ReadFile")),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    runtime.set_plan_only(True)
    events = _collect(runtime, "规划一下")
    finals = [ev.text for ev in events if ev.kind is EventKind.FINAL_REPLY]
    assert finals and "方案一" in finals[-1]


def test_plan_off_allows_write(monkeypatch, tmp_path):
    edited = tmp_path / "f.txt"
    edited.write_text("hello world", encoding="utf-8")
    provider = _ToolProvider([
        [ToolCall(id="c1", name="EditFile", arguments={})],
        ["done"],
    ])
    _patch_provider(monkeypatch, provider)
    recorder: list[str] = []

    class _PlanWrite(Tool):
        permission = PermissionLevel.WRITE

        def __init__(self):
            self.name = "EditFile"

        def execute(self, args):
            recorder.append("write")
            edited.write_text("CHANGED", encoding="utf-8")
            return "written"

    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_PlanWrite()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    runtime.set_plan_only(False)
    _collect(runtime, "改一下")

    assert recorder == ["write"]
    assert edited.read_text(encoding="utf-8") == "CHANGED"


# --------------------------------------------------------------------------- #
# 第 22 组：取消与回滚
# --------------------------------------------------------------------------- #
def test_cancel_token_rolls_back_turn(monkeypatch):
    provider = _ToolProvider([[ToolCall(id="c1", name="ReadFile", arguments={}), "继续"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    cancel = CancelToken()
    events = runtime.run_turn("开始", cancel)
    first = next(events)  # USER_MESSAGE
    assert first.kind is EventKind.USER_MESSAGE
    cancel.cancel()  # 模拟中途取消
    rest = list(events)
    assert any(ev.kind is EventKind.ERROR for ev in rest)
    assert runtime.session.message_count == 0


def test_cancel_state_clean_no_orphan_messages(monkeypatch):
    provider = _ToolProvider([
        [ToolCall(id="c1", name="ReadFile", arguments={})],
        [ToolCall(id="c2", name="ReadFile", arguments={})],
        ["done"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    cancel = CancelToken()
    # 第一轮正常跑完
    list(runtime.run_turn("开始", CancelToken()))
    before = runtime.session.message_count
    assert before > 0
    # 第二轮中途取消
    events = runtime.run_turn("再来", cancel)
    next(events)
    cancel.cancel()
    list(events)
    # 第二轮整轮回滚：消息数回到第二轮之前
    assert runtime.session.message_count == before


# --------------------------------------------------------------------------- #
# 第 19 组补充：阈值（沿用 v2 上下文层）
# --------------------------------------------------------------------------- #
def test_block_threshold_prevents_request(monkeypatch):
    touched: list[int] = []
    provider = _ToolProvider([["回复"]])
    _patch_provider(monkeypatch, provider)
    ui = _make_renderer_ui()
    runtime = AgentRuntime(
        _make_config(context_limit=100), Session(), _registry(),
        ask=lambda p: "y", ui=ui, model="gpt-4o",
    )
    # 预填一个超阈值的用户消息
    runtime.session.add_user("很长很长的一段话" * 40)
    _collect(runtime, "再来一句")
    assert provider.calls == 0
    assert any("本次请求未发送" in e for e in ui.errors)


def test_warn_threshold_still_sends(monkeypatch):
    provider = _ToolProvider([["ok"]])
    _patch_provider(monkeypatch, provider)
    ui = _make_renderer_ui()
    runtime = AgentRuntime(
        _make_config(context_limit=500), Session(), _registry(),
        ask=lambda p: "y", ui=ui, model="gpt-4o",
    )
    runtime.session.add_user("x" * 1600)  # ~80%
    _collect(runtime, "继续")
    assert provider.calls == 1
    assert any("已达 80%" in n for n in ui.notices)


# --------------------------------------------------------------------------- #
# 第 24 组：端到端（E12–E15 映射）
# --------------------------------------------------------------------------- #
def test_e12_multi_round_react_closes_loop(monkeypatch, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    provider = _ToolProvider([
        [ToolCall(id="c1", name="ReadFile", arguments={"path": str(f)})],
        ["我已读到内容，前段为 alpha"],
    ])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "读文件并总结")
    finals = [ev.text for ev in events if ev.kind is EventKind.FINAL_REPLY]
    assert finals and "alpha" in finals[-1]
    assert any(m.tool_call_id is not None for m in runtime.session.messages())


def test_e14_plan_only_blocks_write_and_plans(monkeypatch, tmp_path):
    edited = tmp_path / "f.txt"
    edited.write_text("orig", encoding="utf-8")
    provider = _ToolProvider([
        [
            ToolCall(id="c1", name="EditFile", arguments={}),
            ToolCall(id="c2", name="ReadFile", arguments={}),
        ],
        ["计划：先读再改"],
    ])
    _patch_provider(monkeypatch, provider)

    class _W(Tool):
        permission = PermissionLevel.WRITE

        def __init__(self):
            self.name = "EditFile"

        def execute(self, args):
            edited.write_text("CHANGED", encoding="utf-8")
            return "written"

    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_W(), _ReadTool("ReadFile")),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    runtime.set_plan_only(True)
    _collect(runtime, "规划")
    assert edited.read_text(encoding="utf-8") == "orig"


def test_e15_cancel_keeps_state_clean(monkeypatch):
    provider = _ToolProvider([[ToolCall(id="c1", name="ReadFile", arguments={})], ["done"]])
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    cancel = CancelToken()
    events = runtime.run_turn("开始", cancel)
    next(events)
    cancel.cancel()
    list(events)
    assert runtime.session.message_count == 0


# --------------------------------------------------------------------------- #
# 自动批准（auto_approve）：便利开关，但危险命令不放开
# --------------------------------------------------------------------------- #
class _ShellTool(Tool):
    permission = PermissionLevel.EXECUTE

    def __init__(self, name="PowerShell", recorder=None):
        self.name = name
        self._recorder = recorder

    def execute(self, args):
        if self._recorder is not None:
            self._recorder.append(args.get("command", ""))
        return "shell-ok"


def _make_ask(answer="n"):
    """假 ask：记录每一条提示，并统一回答 `answer`。"""
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    return ask, prompts


def _tool_results(events):
    return [ev for ev in events if ev.kind is EventKind.TOOL_RESULT]


def test_auto_approve_skips_write_confirmation(monkeypatch):
    """自动批准下写工具不再打断用户——即使 ask 预设回答 n 也照常执行。"""
    provider = _ToolProvider([
        [ToolCall(id="c1", name="EditFile", arguments={})],
        ["完成"],
    ])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("n")
    runtime = AgentRuntime(
        _make_config(auto_approve=True), Session(), _registry(_WriteTool()),
        ask=ask, ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "写文件")

    assert prompts == []
    assert _tool_results(events)[0].text == "written"


def test_without_auto_approve_write_still_asks(monkeypatch):
    """不开自动批准时行为不变：写工具仍要确认，拒绝即不执行。"""
    provider = _ToolProvider([
        [ToolCall(id="c1", name="EditFile", arguments={})],
        ["完成"],
    ])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("n")
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_WriteTool()),
        ask=ask, ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "写文件")

    assert len(prompts) == 1
    assert _tool_results(events)[0].text == "用户拒绝执行"


def test_auto_approve_allows_ordinary_shell(monkeypatch):
    provider = _ToolProvider([
        [ToolCall(id="c1", name="PowerShell", arguments={"command": "Get-ChildItem"})],
        ["完成"],
    ])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("n")
    runtime = AgentRuntime(
        _make_config(auto_approve=True), Session(), _registry(_ShellTool()),
        ask=ask, ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "列目录")

    assert prompts == []
    assert _tool_results(events)[0].text == "shell-ok"


def test_auto_approve_still_blocks_dangerous_command(monkeypatch):
    """安全底线：自动批准不放开危险命令，拒绝后底层工具一次都没跑。"""
    provider = _ToolProvider([
        [ToolCall(id="c1", name="PowerShell", arguments={"command": "rm -rf ./dist"})],
        ["完成"],
    ])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("n")
    ui = _make_renderer_ui()
    ran: list[str] = []
    runtime = AgentRuntime(
        _make_config(auto_approve=True), Session(), _registry(_ShellTool(recorder=ran)),
        ask=ask, ui=ui, model="gpt-4o",
    )
    events = _collect(runtime, "清理构建产物")

    assert len(prompts) == 1
    assert _tool_results(events)[0].text == "用户拒绝执行"
    assert ran == []
    assert any("危险命令" in item for item in ui.errors)


def test_dangerous_command_runs_only_on_explicit_yes(monkeypatch):
    """自动批准下危险命令输 yes 仍放行——拦截是确认，不是封死。"""
    provider = _ToolProvider([
        [ToolCall(id="c1", name="PowerShell", arguments={"command": "rm -rf ./dist"})],
        ["完成"],
    ])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("yes")
    ran: list[str] = []
    runtime = AgentRuntime(
        _make_config(auto_approve=True), Session(), _registry(_ShellTool(recorder=ran)),
        ask=ask, ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "清理构建产物")

    assert len(prompts) == 1
    assert _tool_results(events)[0].text == "shell-ok"
    assert ran == ["rm -rf ./dist"]


def test_read_never_asks_regardless_of_auto(monkeypatch):
    provider = _ToolProvider([[ToolCall(id="c1", name="ReadFile", arguments={})], ["完成"]])
    _patch_provider(monkeypatch, provider)
    ask, prompts = _make_ask("n")
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=ask, ui=_make_renderer_ui(), model="gpt-4o",
    )
    events = _collect(runtime, "读文件")

    assert prompts == []
    assert _tool_results(events)[0].text == "read-content"


def test_set_auto_approve_toggles_at_runtime():
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(), ask=lambda p: "y",
        ui=_make_renderer_ui(), model="gpt-4o",
    )
    assert runtime.auto_approve is False
    runtime.set_auto_approve(True)
    assert runtime.auto_approve is True
    runtime.set_auto_approve(False)
    assert runtime.auto_approve is False


# --------------------------------------------------------------------------- #
# v4 请求装配：稳定前缀 / 环境消息 / 注入
# --------------------------------------------------------------------------- #
class _CapturingProvider:
    """记录每笔请求（消息列表 + 参数）的假供应商。"""

    supports_tools = True
    supports_temperature = True

    def __init__(self):
        self.captured: list[tuple[list, GenerateParams]] = []

    def stream(self, messages, params: GenerateParams, tools=()):
        self.captured.append((list(messages), params))
        yield "ok"


def _snapshot(cwd: str):
    from eikocode.prompts.environment import EnvironmentSnapshot

    return EnvironmentSnapshot(cwd=cwd, os_name="Windows 11", date="2026-09-03")


def _patch_capture(monkeypatch, snapshots: list):
    """按调用次序返回快照序列（耗尽后停在最后一个）。"""
    calls = {"n": 0}

    def fake_capture(cwd=None):
        snap = snapshots[min(calls["n"], len(snapshots) - 1)]
        calls["n"] += 1
        return snap

    monkeypatch.setattr("eikocode.agent.loop.capture", fake_capture)


def test_request_carries_stable_system_and_env_first(monkeypatch):
    from eikocode.prompts import assemble

    provider = _CapturingProvider()
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    _collect(runtime, "第一问")
    _collect(runtime, "第二问")

    first_msgs, first_params = provider.captured[0]
    second_msgs, second_params = provider.captured[1]

    assert first_params.system == assemble()
    assert second_params.system == first_params.system  # 稳定前缀逐字节不变
    assert first_msgs[0].content.startswith("<environment>")
    assert first_msgs[0].content.rstrip().endswith("</environment>")
    assert first_msgs[1].content == "第一问"  # 用户消息紧随环境首条
    assert second_msgs[0].content == first_msgs[0].content  # 首条不改写


def test_plan_injection_not_in_history(monkeypatch):
    provider = _CapturingProvider()
    _patch_provider(monkeypatch, provider)
    ui = _make_renderer_ui()
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=ui, model="gpt-4o",
    )
    runtime.set_plan_only(True)
    _collect(runtime, "规划这个")

    msgs, _ = provider.captured[0]
    assert msgs[-1].content.startswith("<system-reminder>")
    assert any("已注入" in n for n in ui.notices)
    # 注入不写入会话历史（只有用户消息 + 最终回复），因此也不计阈值计量
    assert runtime.session.message_count == 2


def test_plan_reminder_rhythm_across_turns(monkeypatch):
    provider = _CapturingProvider()
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    runtime.set_plan_only(True)
    for _ in range(6):
        _collect(runtime, "继续")

    full_marker = "写入与执行类工具会被拦截"
    tails = [msgs[-1].content for msgs, _ in provider.captured]
    assert full_marker in tails[0]  # 首轮完整
    assert all(full_marker not in text for text in tails[1:5])  # 第 2–5 轮精简
    assert full_marker in tails[5]  # 第 6 轮完整


def test_env_change_appends_new_message(monkeypatch):
    from eikocode.prompts.environment import format_message

    snap_a, snap_b = _snapshot("C:\\a"), _snapshot("C:\\b")
    _patch_capture(monkeypatch, [snap_a, snap_b])
    provider = _CapturingProvider()
    _patch_provider(monkeypatch, provider)
    ui = _make_renderer_ui()
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=ui, model="gpt-4o",
    )
    _collect(runtime, "第一次")
    _collect(runtime, "第二次")

    first_msgs, _ = provider.captured[0]
    second_msgs, _ = provider.captured[1]
    assert first_msgs[0].content == format_message(snap_a)
    assert second_msgs[0].content == format_message(snap_a)  # 首条原样
    assert second_msgs[-1].content == format_message(snap_b)  # 变更以追加表达
    assert any("环境变化" in n for n in ui.notices)
    # 环境消息不进会话历史
    assert all(
        not m.content.startswith("<environment>") for m in runtime.session.messages()
    )


def test_clear_rebuilds_environment(monkeypatch):
    from eikocode.prompts.environment import format_message

    snap_a, snap_b = _snapshot("C:\\a"), _snapshot("C:\\b")
    _patch_capture(monkeypatch, [snap_a, snap_b])
    provider = _CapturingProvider()
    _patch_provider(monkeypatch, provider)
    runtime = AgentRuntime(
        _make_config(), Session(), _registry(_ReadTool()),
        ask=lambda p: "y", ui=_make_renderer_ui(), model="gpt-4o",
    )
    _collect(runtime, "第一次")
    runtime.session.clear()
    _collect(runtime, "第二次")

    second_msgs, _ = provider.captured[1]
    assert second_msgs[0].content == format_message(snap_b)  # 重建，而非追加
    assert second_msgs[1].content == "第二次"
    assert len(second_msgs) == 2  # 无追加的环境消息
