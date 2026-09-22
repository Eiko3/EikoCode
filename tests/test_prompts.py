"""提示词装配测试（离线，v4）。

覆盖 checklist.md 组 26–31 与供应商侧 payload 形状：
- 模块与拼装：6 模块、优先级顺序、单条文本、确定性、可插入
- 环境消息：标签格式、字段、快照
- 运行时注入：标签包裹、节奏
- 双重强化：工具描述与全局指令两侧都有规则
- 供应商：system 通道、缓存断点、连续同角色合并、usage 缓存字段解析
"""

from __future__ import annotations

from types import SimpleNamespace

from eikocode.prompts import (
    PLAN_BRIEF,
    PLAN_FULL,
    PLAN_FULL_INTERVAL,
    assemble,
    capture,
    format_message,
    plan_reminder,
    reminder,
)
from eikocode.prompts.environment import ENV_CLOSE, ENV_OPEN
from eikocode.prompts.injection import REMINDER_CLOSE, REMINDER_OPEN
from eikocode.prompts.modules import (
    BEHAVIOR,
    CODE_STYLE,
    IDENTITY,
    MODULE_SEQUENCE,
    OUTPUT_STYLE,
    SAFETY,
    TOOL_USE,
)
from eikocode.providers.anthropic import _to_anthropic_messages
from eikocode.providers.base import GenerateParams, Message, Role, ToolCall, UsageInfo
from eikocode.providers.openai_compat import OpenAICompatProvider
from eikocode.tools import get_registry

# --------------------------------------------------------------------------- #
# 组 26：模块与拼装器
# --------------------------------------------------------------------------- #
def test_modules_six_with_minimal_content():
    assert len(MODULE_SEQUENCE) == 6
    for module in MODULE_SEQUENCE:
        assert len(module.strip()) >= 20  # 最小可用内容，非占位


def test_priority_order_identity_before_output_style():
    text = assemble()
    assert text.index(IDENTITY.strip()[:10]) < text.index(OUTPUT_STYLE.strip()[:10])
    assert text.index(SAFETY.strip()[:10]) < text.index(BEHAVIOR.strip()[:10])
    assert text.index(TOOL_USE.strip()[:10]) < text.index(CODE_STYLE.strip()[:10])


def test_assemble_returns_single_text():
    assert isinstance(assemble(), str)
    assert "<module>" not in assemble()  # 无内部分隔结构


def test_assembly_is_deterministic():
    assert assemble() == assemble()
    # 打乱对象身份后（同一序列重新构造）仍逐字节一致
    assert assemble(list(MODULE_SEQUENCE)) == assemble(tuple(MODULE_SEQUENCE))


def test_new_module_inserted_by_priority():
    marker = "TEST-MODULE-CONTENT"
    reordered = (IDENTITY, SAFETY, marker, TOOL_USE, BEHAVIOR, CODE_STYLE, OUTPUT_STYLE)
    text = assemble(reordered)
    assert text.index(marker) < text.index(TOOL_USE.strip()[:10])
    assert text.index(marker) > text.index(SAFETY.strip()[:10])


# --------------------------------------------------------------------------- #
# 组 27：环境消息
# --------------------------------------------------------------------------- #
def test_env_message_has_tags_and_fields():
    snap = capture("C:\\proj")
    text = format_message(snap)
    assert text.startswith(ENV_OPEN)
    assert text.rstrip().endswith(ENV_CLOSE)
    for field in ("工作目录：C:\\proj", "操作系统：", "日期："):
        assert field in text


def test_capture_prefers_given_cwd():
    snap = capture("X:\\elsewhere")
    assert snap.cwd == "X:\\elsewhere"
    assert snap.os_name  # 其余字段来自真实探测


def test_snapshot_equality_means_unchanged():
    a = capture("C:\\proj")
    b = capture("C:\\proj")
    assert a == b  # cwd 相同且同一天 → 未变化


# --------------------------------------------------------------------------- #
# 组 29：运行时注入
# --------------------------------------------------------------------------- #
def test_reminder_wraps_in_tags():
    text = reminder("做某事")
    assert text.startswith(REMINDER_OPEN)
    assert text.rstrip().endswith(REMINDER_CLOSE)
    assert "做某事" in text


def test_plan_reminder_rhythm():
    # 第 1 轮完整；第 2–5 轮精简；第 6 轮完整；第 7–10 轮精简；第 11 轮完整
    full_marker = "写入与执行类工具会被拦截"
    for turn in (1, 6, 11):
        assert full_marker in plan_reminder(turn), turn
    for turn in (2, 3, 4, 5, 7, 8, 9, 10):
        text = plan_reminder(turn)
        assert full_marker not in text, turn
        assert PLAN_BRIEF in text, turn


def test_plan_interval_is_five():
    assert PLAN_FULL_INTERVAL == 5
    assert "只读" in PLAN_BRIEF
    assert "计划" in PLAN_FULL


# --------------------------------------------------------------------------- #
# 组 31：双重强化
# --------------------------------------------------------------------------- #
def test_write_tools_require_reading_first():
    registry = get_registry()
    assert "先读" in registry.get("EditFile").description
    assert "先读" in registry.get("WriteFile").description


def test_shell_prefers_dedicated_tools():
    registry = get_registry()
    assert "专用工具" in registry.get("Shell").description


def test_tool_use_module_reinforces_same_rules():
    assert "先读" in TOOL_USE
    assert "专用工具" in TOOL_USE


def test_tool_specs_are_stable_text():
    """工具描述参与缓存前缀，必须稳定（无动态内容）。"""
    registry = get_registry()
    first = [(t.name, t.spec().description, t.spec().input_schema) for t in registry.all()]
    second = [(t.name, t.spec().description, t.spec().input_schema) for t in registry.all()]
    assert first == second


# --------------------------------------------------------------------------- #
# 组 28：供应商 payload 形状（system 通道 / 断点 / 合并 / usage）
# --------------------------------------------------------------------------- #
class _FakeCompletions:
    def __init__(self, capture: dict, chunks: list):
        self._capture = capture
        self._chunks = chunks

    def create(self, **kwargs):
        self._capture.clear()
        self._capture.update(kwargs)
        return iter(self._chunks)


class _FakeOpenAI:
    def __init__(self, capture: dict, chunks: list):
        self.chat = SimpleNamespace(completions=_FakeCompletions(capture, chunks))


def _text_chunk(text: str):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _usage_chunk():
    usage = SimpleNamespace(
        prompt_tokens=200,
        completion_tokens=5,
        prompt_cache_hit_tokens=100,
        prompt_cache_miss_tokens=100,
    )
    return SimpleNamespace(choices=[], usage=usage)


def test_openai_system_is_first_message():
    captured: dict = {}
    client = _FakeOpenAI(captured, [_text_chunk("hi"), _usage_chunk()])
    provider = OpenAICompatProvider(api_key="k", client=client)
    params = GenerateParams(model="deepseek-chat", temperature=0.7, max_tokens=8, system="SYS")
    items = list(provider.stream([Message(role=Role.USER, content="hi")], params))

    messages = captured["messages"]
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1] == {"role": "user", "content": "hi"}
    # 缓存字段被请求（流式 usage 不请求就没有）
    assert captured["stream_options"] == {"include_usage": True}
    # usage 块被解析为归一表示
    usage = [i for i in items if isinstance(i, UsageInfo)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 200
    assert usage[0].cache_hit == 100


def test_openai_without_system_keeps_plain_payload():
    captured: dict = {}
    client = _FakeOpenAI(captured, [_text_chunk("hi")])
    provider = OpenAICompatProvider(api_key="k", client=client)
    params = GenerateParams(model="deepseek-chat", temperature=0.7, max_tokens=8)
    list(provider.stream([Message(role=Role.USER, content="hi")], params))
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


class _FakeAnthropicMessages:
    def __init__(self, capture: dict, events: list):
        self._capture = capture
        self._events = events

    def stream(self, **kwargs):
        self._capture.clear()
        self._capture.update(kwargs)
        return self

    def __enter__(self):
        return self._events

    def __exit__(self, *args):
        return False


def test_anthropic_system_carries_single_cache_breakpoint():
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=300,
                    cache_read_input_tokens=250,
                    cache_creation_input_tokens=50,
                )
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="ok"),
        ),
    ]
    captured: dict = {}
    client = SimpleNamespace(messages=_FakeAnthropicMessages(captured, events))
    from eikocode.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="k", client=client)
    params = GenerateParams(model="claude-x", temperature=0.7, max_tokens=8, system="SYS")
    items = list(provider.stream([Message(role=Role.USER, content="hi")], params))

    system = captured["system"]
    assert system[0]["text"] == "SYS"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert len(system) == 1  # 只有一个断点
    usage = [i for i in items if isinstance(i, UsageInfo)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 300
    assert usage[0].cache_hit == 250
    assert usage[0].cache_write == 50


def test_anthropic_merges_consecutive_user_messages():
    """工具结果（user 角色）之后跟注入（user 角色）必须合并，否则 API 400。"""
    messages = [
        Message(role=Role.USER, content="改一下"),
        Message(role=Role.ASSISTANT, content="", tool_calls=(ToolCall(id="c1", name="ReadFile", arguments={}),)),
        Message(role=Role.USER, content="文件内容…", tool_call_id="c1"),
        Message(role=Role.USER, content="<system-reminder>\n提醒\n</system-reminder>"),
    ]
    payload = _to_anthropic_messages(messages)
    roles = [p["role"] for p in payload]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    # 合并块顺序保持：tool_result 在前、文本在后
    last_blocks = payload[-1]["content"]
    types = [b["type"] for b in last_blocks]
    assert types == ["tool_result", "text"]
    assert last_blocks[1]["text"].startswith("<system-reminder>")


def test_anthropic_plain_history_unchanged():
    messages = [
        Message(role=Role.USER, content="你好"),
        Message(role=Role.ASSISTANT, content="你好！"),
        Message(role=Role.USER, content="再见"),
    ]
    payload = _to_anthropic_messages(messages)
    assert [p["role"] for p in payload] == ["user", "assistant", "user"]
    assert payload[0]["content"] == "你好"
