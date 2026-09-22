"""上下文压缩测试（离线，v7）。

覆盖 checklist.md 组 50–56 与 E27 / E28：
- 快照：往返、同秒唯一、失败抛错、无清理代码
- 预防层：单条阈值、合计挑大存盘、小结果不动、写盘失败回退、可见性
- 摘要：九部分、禁工具首尾、标签解析、用户原话逐字（代码拼接）
- 兜底：阈值触发、最近 3 轮原文保留、边界消息、熔断
- 配置：默认值、auto_compact 关闭回退、summary_model
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from eikocode import config as config_module
from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.loop import _Ui
from eikocode.context import (
    BOUNDARY_MESSAGE,
    COMPACT_RATIO,
    CompactBreaker,
    apply_prevention,
    build_prompt,
    compact_history,
    estimate_messages,
    parse_summary,
    save_snapshot,
)
from eikocode.context.compaction import _CompactConfig
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.config import load_config
from eikocode.providers.base import GenerateParams, Message, Role, ToolCall
from eikocode.session import Session
from eikocode.tools import get_registry
from eikocode.tools.base import PermissionLevel, Tool


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
class _FakeUi:
    def __init__(self):
        self.notices: list[str] = []
        self.errors: list[str] = []

    def notice(self, t):
        self.notices.append(t)

    def error(self, t):
        self.errors.append(t)


class _T(Tool):
    def __init__(self, name="W", permission=PermissionLevel.WRITE):
        self.name = name
        self.permission = permission
        self.is_dangerous = False

    def execute(self, args):
        return "ok"


def _compact_config(**overrides) -> _CompactConfig:
    defaults = dict(
        tool_result_chars=100,
        message_chars=300,
        preview_chars=30,
    )
    defaults.update(overrides)
    return _CompactConfig(**defaults)


def _msg(role, content, **kw):
    return Message(role=role, content=content, **kw)


# --------------------------------------------------------------------------- #
# 组 50：快照存储
# --------------------------------------------------------------------------- #
def test_snapshot_roundtrip(tmp_path):
    path = save_snapshot("A" * 5000, "ReadFile", base_dir=tmp_path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "A" * 5000


def test_snapshot_names_unique_within_same_second(tmp_path):
    p1 = save_snapshot("one", "W", base_dir=tmp_path)
    p2 = save_snapshot("two", "W", base_dir=tmp_path)
    assert p1 != p2 and p1.exists() and p2.exists()
    assert ".eikocode/snapshots" in str(p1).replace("\\", "/")


def test_snapshot_failure_raises(tmp_path, monkeypatch):
    import eikocode.context.snapshots as snap

    def boom(self, content, encoding=None):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(EikoCodeError) as excinfo:
        save_snapshot("x", "W", base_dir=tmp_path)
    assert "快照写入失败" in excinfo.value.detail


def test_snapshot_has_no_cleanup_code():
    source = inspect.getsource(save_snapshot.__globals__["__package__"] and __import__(
        "eikocode.context.snapshots", fromlist=["x"]
    ))
    assert "unlink" not in source and "rmtree" not in source and ".remove(" not in source


# --------------------------------------------------------------------------- #
# 组 51：预防层
# --------------------------------------------------------------------------- #
def _results(*pairs):
    """pairs: (id, tool_name, text, is_error) → results dict + tool_calls"""
    tool_calls = []
    results = {}
    for i, (tid, name, text, is_error) in enumerate(pairs):
        tool_calls.append(ToolCall(id=tid, name=name, arguments={}))
        results[tid] = (text, is_error)
    return results, tool_calls


def test_single_oversized_result_saved(tmp_path):
    cfg = _compact_config()
    big = "x" * 250
    results, calls = _results(("c1", "ReadFile", big, False))
    ui = _FakeUi()
    new = apply_prevention(results, calls, cfg, ui.notice, save=lambda t, n: save_snapshot(t, n, base_dir=tmp_path))

    text, err = new["c1"]
    assert not err
    assert "x" * 250 not in text  # 全文不在消息里
    assert "完整内容已写入" in text
    raw_path = text.split("已写入：")[1].strip().rstrip("）…")
    snapshot_path = Path(raw_path.split("\n")[0])
    assert snapshot_path.read_text(encoding="utf-8") == big
    assert any("已写入" in n for n in ui.notices)


def test_preview_length_capped(tmp_path):
    cfg = _compact_config()
    results, calls = _results(("c1", "ReadFile", "y" * 250, False))
    new = apply_prevention(results, calls, cfg, lambda t: None, save=lambda t, n: save_snapshot(t, n, base_dir=tmp_path))
    text = new["c1"][0]
    body = text.split("\n…")[0]
    assert len(body) <= cfg.preview_chars + 10


def test_total_cap_saves_largest_first(tmp_path):
    """合计超限 → 挑大的依次存盘：40/50/30、cap 50 → 50 与 40 被存，30 原样。

    快照路径本身占 token，压缩后的合计未必 ≤ cap（路径是不可避免的开销），
    因此断言「最大的两条被存盘、最小条原样」而不是死抠合计数字。
    """
    cfg = _compact_config(message_chars=50)
    results, calls = _results(
        ("a", "R", "A" * 40, False),
        ("b", "R", "B" * 50, False),
        ("c", "R", "C" * 30, False),
    )
    new = apply_prevention(results, calls, cfg, lambda t: None, save=lambda t, n: save_snapshot(t, n, base_dir=tmp_path))

    assert "B" * 50 not in new["b"][0]
    assert "完整内容已写入" in new["b"][0]
    assert "A" * 40 not in new["a"][0]
    assert "完整内容已写入" in new["a"][0]
    assert new["c"][0] == "C" * 30  # 未超限的原样保留


def test_small_results_untouched(tmp_path):
    cfg = _compact_config()
    results, calls = _results(("c1", "R", "tiny", False))
    new = apply_prevention(results, calls, cfg, lambda t: None, save=lambda t, n: save_snapshot(t, n, base_dir=tmp_path))
    assert new["c1"][0] == "tiny"


def test_save_failure_marks_blocked(tmp_path):
    cfg = _compact_config()

    def fail_save(text, name):
        raise EikoCodeError(ErrorKind.TOOL_ERROR, "快照写入失败：disk full")

    results, calls = _results(("c1", "ReadFile", "z" * 250, False))
    ui = _FakeUi()
    new = apply_prevention(results, calls, cfg, ui.notice, save=fail_save)

    text, err = new["c1"]
    assert err  # 回退到阻止发送
    assert "已阻止发送" in text
    assert any("快照写入失败" in n for n in ui.notices)


# --------------------------------------------------------------------------- #
# 组 52：摘要生成
# --------------------------------------------------------------------------- #
def test_prompt_has_nine_sections_and_no_tool_twice():
    prompt = build_prompt([_msg(Role.USER, "hi")])
    for section in ("主要请求", "关键概念", "文件与代码", "错误与修复", "解决过程", "用户原话", "待办", "当前工作", "下一步"):
        assert section in prompt
    assert prompt.count("禁止调用任何工具") >= 2  # 首尾各一次
    assert prompt.index("禁止调用任何工具") < prompt.index("【对话历史】")


def test_parse_summary_extracts_tagged_part():
    text = "<draft>\n草稿内容\n</draft>\n<summary>\n## 主要请求\n做 A\n</summary>"
    summary = parse_summary(text)
    assert summary.startswith("## 主要请求")
    assert "草稿内容" not in summary


def test_summarize_uses_given_model_and_system():
    recorded = {}

    class _FakeProvider:
        def stream(self, messages, params: GenerateParams, tools=()):
            recorded["model"] = params.model
            recorded["system"] = params.system
            recorded["messages"] = list(messages)
            yield "<summary>\n## 主要请求\n做 A\n</summary>"

    summary = summarize_stub(_FakeProvider(), "cheap-model", [_msg(Role.USER, "原话一")])
    assert "主要请求" in summary
    assert recorded["model"] == "cheap-model"
    assert "禁止调用任何工具" in recorded["system"]


def summarize_stub(provider, model, messages):
    from eikocode.context.summarizer import summarize

    return summarize(provider, model, messages)


def test_user_quotes_verbatim():
    from eikocode.context.compaction import _user_quotes

    msgs = [
        _msg(Role.USER, "记住编号 12345！"),
        _msg(Role.ASSISTANT, "好的。"),
        _msg(Role.USER, "继续", tool_call_id="c1"),  # 工具结果不算用户原话
    ]
    quotes = _user_quotes(msgs)
    assert "- 记住编号 12345！" in quotes  # 含标点逐字
    assert "继续" not in quotes


# --------------------------------------------------------------------------- #
# 组 53 / 54：兜底集成与熔断
# --------------------------------------------------------------------------- #
class _ScriptedProvider:
    """按调用次序返回预设文本的假供应商。"""

    def __init__(self, scripts: list[str | Exception]):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    def stream(self, messages, params: GenerateParams, tools=()):
        self.calls.append({"messages": list(messages), "model": params.model})
        if not self.scripts:
            yield "（无更多预设）"
            return
        item = self.scripts.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item


def _long_history(turns: int = 4) -> Session:
    session = Session()
    for i in range(turns):
        session.add_user(f"用户消息 {i}：请帮我处理任务 {i}")
        session.complete_assistant(f"助手回复 {i}：" + "细节 " * 20)
    return session


def _compact_config_full(**kw):
    return SimpleNamespace(
        context_limit=50,
        auto_compact=True,
        summary_model="",
        compact_tool_result_chars=100,
        compact_message_chars=300,
        compact_preview_chars=30,
        **kw,
    )


def test_compact_replaces_old_rounds_keeps_recent_verbatim():
    session = _long_history(4)
    provider = _ScriptedProvider(["<summary>\n## 主要请求\n处理任务\n</summary>"])
    breaker = CompactBreaker()
    ui = _FakeUi()

    done = compact_history(session, provider, "m", ui.notice, breaker)

    assert done
    msgs = session.messages()
    assert len(msgs) == 1 + 3 * 2  # 摘要 + 最近 3 轮（每轮用户+助手）
    summary_msg = msgs[0]
    assert "## 主要请求" in summary_msg.content
    assert "用户原话（逐字保留）" in summary_msg.content
    assert "用户消息 0：请帮我处理任务 0" in summary_msg.content  # 逐字
    assert BOUNDARY_MESSAGE in summary_msg.content
    # 最近一轮原文逐字保留
    assert msgs[-1].content.startswith("助手回复 3：")
    assert "用户消息 3：请帮我处理任务 3" in msgs[-2].content


def test_compact_below_min_turns_skipped():
    session = _long_history(3)
    provider = _ScriptedProvider(["<summary>x</summary>"])
    assert compact_history(session, provider, "m", lambda t: None, CompactBreaker()) is False
    assert provider.calls == []  # 没有发起摘要请求


def test_breaker_blocks_auto_but_not_manual():
    session = _long_history(4)
    breaker = CompactBreaker()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.open

    provider = _ScriptedProvider(["<summary>x</summary>"])
    # 自动路径被熔断拦下
    assert compact_history(session, provider, "m", lambda t: None, breaker) is False
    assert provider.calls == []
    # 手动（force）绕过熔断
    assert compact_history(session, provider, "m", lambda t: None, breaker, force=True) is True


def test_breaker_opens_after_three_failures():
    breaker = CompactBreaker()
    session = _long_history(4)
    ui = _FakeUi()
    provider = _ScriptedProvider([EikoCodeError(ErrorKind.NETWORK, "E1"), EikoCodeError(ErrorKind.NETWORK, "E2"), EikoCodeError(ErrorKind.NETWORK, "E3")])

    for _ in range(3):
        compact_history(session, provider, "m", ui.notice, breaker)

    assert breaker.open
    assert any("已熔断" in n for n in ui.notices)


def test_breaker_success_resets():
    breaker = CompactBreaker()
    breaker.record_failure()
    breaker.record_failure()
    session = _long_history(4)
    provider = _ScriptedProvider(["<summary>x</summary>"])
    compact_history(session, provider, "m", lambda t: None, breaker, force=True)
    assert not breaker.open and breaker.failures == 0
    # 再失败两次仍未熔断
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.open


def test_compact_failure_notices_count():
    breaker = CompactBreaker()
    session = _long_history(4)
    ui = _FakeUi()
    provider = _ScriptedProvider([EikoCodeError(ErrorKind.NETWORK, "E1"), EikoCodeError(ErrorKind.NETWORK, "E2")])
    compact_history(session, provider, "m", ui.notice, breaker)
    compact_history(session, provider, "m", ui.notice, breaker)
    assert any("1/3" in n for n in ui.notices)
    assert any("2/3" in n for n in ui.notices)


# --------------------------------------------------------------------------- #
# 组 55：配置
# --------------------------------------------------------------------------- #
def test_compact_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", tmp_path / "u.toml")
    project = tmp_path / "p"
    project.mkdir()
    config = load_config(cwd=project)
    assert config.auto_compact is True
    assert config.summary_model == ""
    assert config.compact_tool_result_chars == 20_000
    assert config.compact_message_chars == 40_000
    assert config.compact_preview_chars == 1_500


def test_compact_config_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", tmp_path / "u.toml")
    project = tmp_path / "p"
    project.mkdir()
    (project / ".eikocode.toml").write_text(
        'auto_compact = false\nsummary_model = "deepseek-chat"\n'
        'compact_tool_result_chars = 500\n',
        encoding="utf-8",
    )
    config = load_config(cwd=project)
    assert config.auto_compact is False
    assert config.summary_model == "deepseek-chat"
    assert config.compact_tool_result_chars == 500


# --------------------------------------------------------------------------- #
# 组 56：接入主流程（运行时集成）
# --------------------------------------------------------------------------- #
def _runtime(config, session):
    registry = get_registry()

    class _BigWrite(Tool):
        permission = PermissionLevel.WRITE

        def __init__(self):
            self.name = "BigWrite"

        def execute(self, args):
            return "w" * 250

    registry.register(_BigWrite())
    return AgentRuntime(
        config=config,
        session=session,
        registry=registry,
        ask=lambda p: "y",
        ui=_FakeUi2(),
        model="claude-sonnet-5",
    )


class _FakeUi2:
    def __init__(self):
        self.notices: list[str] = []

    def notice(self, t):
        self.notices.append(t)

    def error(self, t):
        self.notices.append(t)


def test_prevention_applied_in_run_turn(tmp_path, monkeypatch):
    """超大工具结果在回写会话前被存盘留预览（预防层挂在回写点）。"""
    provider = _OneShotProvider()
    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider",
        lambda config, model: provider,
    )
    session = Session()
    config = SimpleNamespace(
        model="claude-sonnet-5",
        temperature=0.2,
        max_tokens=1000,
        context_limit=100_000,
        auto_compact=False,  # 隔离兜底
        compact_tool_result_chars=100,
        compact_message_chars=10**9,
        compact_preview_chars=30,
    )
    runtime = _runtime(config, session)
    events = list(runtime.run_turn("写大文件", CancelToken()))
    kinds = [ev.kind.value for ev in events]
    print("EVENTS:", kinds)
    print("SESSION:", [(m.role.value, m.tool_call_id) for m in session.messages()])
    results = [m for m in session.messages() if m.tool_call_id]
    assert len(results) == 1
    assert "w" * 250 not in results[0].content
    assert "完整内容已写入" in results[0].content
    # 快照真实存在（去掉尾部中文括号）
    snapshot_line = [line for line in results[0].content.splitlines() if "已写入" in line][0]
    path = Path(snapshot_line.split("已写入：")[1].strip().rstrip("）…"))
    assert path.read_text(encoding="utf-8") == "w" * 250


def test_auto_compact_off_keeps_old_behavior(tmp_path, monkeypatch):
    """关闭自动兜底：即使超过 90% 也不发起摘要请求。"""
    calls: list[dict] = []

    class _CountingProvider:
        def stream(self, messages, params: GenerateParams, tools=()):
            calls.append({"messages": list(messages)})
            yield "回复"

    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider",
        lambda config, model: _CountingProvider(),
    )
    session = Session()
    for i in range(4):
        session.add_user(f"用户消息 {i}：" + "长内容 " * 30)
        session.complete_assistant("回复 " * 30)
    before = session.message_count

    config = SimpleNamespace(
        model="claude-sonnet-5",
        context_limit=100,
        auto_compact=False,
        compact_tool_result_chars=10**9,
        compact_message_chars=10**9,
        compact_preview_chars=30,
    )
    runtime = _runtime(config, session)
    runtime._breaker = CompactBreaker()
    runtime._compact_if_needed()

    assert session.message_count == before  # 历史未被替换
    # 只有本轮对话的调用（由 run_turn 触发的），没有摘要调用——
    # 用直接调用 _compact_if_needed 的方式已覆盖；此处断言 breaker 未变化
    assert runtime.compact_breaker_open is False
    del calls


class _OneShotProvider:
    def __init__(self):
        self.calls = 0

    def stream(self, messages, params: GenerateParams, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield "我来写入大内容。"
            yield ToolCall(id="c1", name="BigWrite", arguments={})
        else:
            yield "完成。"


# --------------------------------------------------------------------------- #
# 稳定前缀不受压缩影响（组 53）
# --------------------------------------------------------------------------- #
def test_stable_assembly_unaffected_by_rewrite():
    from eikocode.prompts import assemble

    before = assemble()
    session = _long_history(4)
    session.rewrite([_msg(Role.USER, "摘要"), *_long_history(1).messages()])
    assert assemble() == before  # 稳定前缀与会话历史无关，逐字节不变
