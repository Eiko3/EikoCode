"""项目指令与会话记忆测试（离线，v8）。

覆盖 checklist.md 组 58–64 与 E31：
- 指令文件：两级拼接顺序、@include（展开/深度/循环/越界/缺失）、空静默
- 注入：请求最早位置、逐字一致、会话内固定
- 存档：JSONL 行数、meta 字段、崩溃截断恢复
- 恢复：坏行跳过、悬空工具调用截断、时间跨度提醒
- 笔记：两级写入、触发计数、失败静默、不进历史
- 命令：/sessions、/resume、/notes
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.loop import _Ui
from eikocode.errors import EikoCodeError
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.memory import (
    INSTRUCTION_FILENAME,
    NotesManager,
    SessionArchiver,
    list_archives,
    load_archive,
    load_instructions,
)
from eikocode.memory.archive import _truncate_unpaired, stale_days
from eikocode.memory.instructions import _expand_includes
from eikocode.providers.base import GenerateParams, Message, Role, ToolCall
from eikocode.session import Session
from eikocode.tools import get_registry
from eikocode.tools.base import PermissionLevel, Tool


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
class _FakeUi:
    def __init__(self):
        self.notices: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []

    def notice(self, t):
        self.notices.append(t)

    def info(self, t):
        self.infos.append(t)

    def error(self, t):
        self.errors.append(t)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 组 58：指令文件
# --------------------------------------------------------------------------- #
def test_no_instruction_files_returns_empty(tmp_path):
    content, loaded = load_instructions(tmp_path / "proj", tmp_path / "user")
    assert content == "" and loaded == []


def test_project_level_comes_before_user_level(tmp_path):
    _write(tmp_path / "proj" / INSTRUCTION_FILENAME, "项目级规则")
    _write(tmp_path / "user" / INSTRUCTION_FILENAME, "用户级规则")
    content, loaded = load_instructions(tmp_path / "proj", tmp_path / "user")
    assert content.index("项目级规则") < content.index("用户级规则")
    assert len(loaded) == 2


def test_instruction_injected_verbatim(monkeypatch):
    from eikocode.prompts import assemble

    class _Provider:
        def stream(self, messages, params: GenerateParams, tools=()):
            recorded["messages"] = list(messages)
            yield "ok"

    recorded: dict = {}
    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider", lambda config, model: _Provider()
    )
    runtime = AgentRuntime(
        config=SimpleNamespace(
            model="m", temperature=0.2, max_tokens=100, context_limit=100_000,
            auto_compact=False,
        ),
        session=Session(),
        registry=get_registry(),
        ask=lambda p: "y",
        ui=_FakeUi(),
        model="m",
        instructions="项目必须使用 tabs 缩进。",
    )
    list(runtime.run_turn("hi", CancelToken()))

    first = recorded["messages"][0]
    assert first.content == "项目必须使用 tabs 缩进。"
    assert recorded["messages"][1].content.startswith("<environment>")  # 环境首条紧随


# --------------------------------------------------------------------------- #
# 组 59：@include
# --------------------------------------------------------------------------- #
def test_include_expands_content(tmp_path):
    _write(tmp_path / "glossary.md", "术语表内容")
    main = _write(tmp_path / "EIKOCODE.md", "主指令\n@glossary.md")
    expanded = _expand_includes(main.read_text(encoding="utf-8"), tmp_path, tmp_path)
    assert "术语表内容" in expanded


def test_include_depth_limit(tmp_path):
    # 6 层链：a→b→c→d→e→f，深度 5 处截断
    for name, nxt in [("f", None), ("e", "f"), ("d", "e"), ("c", "d"), ("b", "c"), ("a", "b")]:
        body = f"@{nxt}.md" if nxt else "最深层"
        _write(tmp_path / f"{name}.md", body)
    expanded = _expand_includes("@a.md", tmp_path, tmp_path)
    assert "最深层" not in expanded


def test_include_cycle_detected(tmp_path):
    _write(tmp_path / "a.md", "A\n@b.md")
    _write(tmp_path / "b.md", "B\n@a.md")
    expanded = _expand_includes("@a.md", tmp_path, tmp_path)
    assert "A" in expanded and "B" in expanded
    assert expanded.count("A") == 1 and expanded.count("B") == 1


def test_include_out_of_bounds_blocked(tmp_path):
    _write(tmp_path.parent / "secret.txt", "机密")
    expanded = _expand_includes("@..\\secret.txt", tmp_path, tmp_path)
    assert "机密" not in expanded


def test_include_missing_line_skipped(tmp_path):
    expanded = _expand_includes("前文\n@missing.md\n后文", tmp_path, tmp_path)
    assert "前文" in expanded and "后文" in expanded
    assert "missing" not in expanded


# --------------------------------------------------------------------------- #
# 组 60：会话存档
# --------------------------------------------------------------------------- #
def test_archive_jsonl_and_meta(tmp_path):
    archiver = SessionArchiver(base_dir=tmp_path)
    archiver.append(Message(role=Role.USER, content="第一问：这是标题"))
    archiver.append(Message(role=Role.USER, content="r", tool_call_id="c1"))
    archiver.append(Message(role=Role.ASSISTANT, content="答", tool_calls=(ToolCall(id="c1", name="R", arguments={}),)))

    lines = archiver.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line) for line in lines)

    meta = json.loads(archiver.meta_path.read_text(encoding="utf-8"))
    assert meta["id"] == archiver.session_id
    assert meta["message_count"] == 3
    assert meta["title"].startswith("第一问")
    assert "created_at" in meta and "last_active_at" in meta


def test_archive_crash_truncation_recovery(tmp_path):
    archiver = SessionArchiver(base_dir=tmp_path)
    archiver.append(Message(role=Role.USER, content="第一问"))
    archiver.append(Message(role=Role.USER, content="第二问"))
    # 模拟崩溃：最后一行写了一半
    with open(archiver.path, "a", encoding="utf-8") as handle:
        handle.write('{"role": "user", "con')

    messages, _ = load_archive(archiver.session_id, base_dir=tmp_path)
    assert [m.content for m in messages] == ["第一问", "第二问"]


# --------------------------------------------------------------------------- #
# 组 61：会话恢复
# --------------------------------------------------------------------------- #
def test_load_archive_skips_bad_lines(tmp_path):
    archiver = SessionArchiver(base_dir=tmp_path)
    archiver.append(Message(role=Role.USER, content="有效"))
    with open(archiver.path, "a", encoding="utf-8") as handle:
        handle.write("{{{{不是 JSON\n")
        handle.write("[1,2,3]\n")
    messages, _ = load_archive(archiver.session_id, base_dir=tmp_path)
    assert [m.content for m in messages] == ["有效"]


def test_load_archive_truncates_unpaired_tool_calls(tmp_path):
    archiver = SessionArchiver(base_dir=tmp_path)
    archiver.append(Message(role=Role.USER, content="问"))
    archiver.append(
        Message(role=Role.ASSISTANT, content="", tool_calls=(ToolCall(id="c1", name="R", arguments={}),))
    )
    # 工具结果行丢失（悬空工具调用）
    messages, _ = load_archive(archiver.session_id, base_dir=tmp_path)
    # 截断到最后完整位置：只剩用户消息
    assert [m.content for m in messages] == ["问"]
    assert not any(m.tool_calls for m in messages)


def test_stale_days_detection():
    from datetime import datetime, timedelta

    now = datetime(2026, 9, 8, 12, 0, 0)
    assert stale_days({"last_active_at": (now - timedelta(days=3)).isoformat()}, now) == 3
    assert stale_days({"last_active_at": now.isoformat()}, now) == 0
    assert stale_days({}, now) is None


def test_missing_archive_raises(tmp_path):
    import pytest

    with pytest.raises(EikoCodeError):
        load_archive("no-such-id", base_dir=tmp_path)


from eikocode.errors import EikoCodeError  # noqa: E402  （置于用例之后亦可用，保持顶部整洁）


# --------------------------------------------------------------------------- #
# 组 62：自动笔记
# --------------------------------------------------------------------------- #
def _notes_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config = SimpleNamespace(model="m", summary_model="")
    manager = NotesManager(
        config, lambda t: None, project_dir=tmp_path / "proj", user_dir=tmp_path / "home" / ".eikocode"
    )
    return manager


def test_notes_two_level_paths(tmp_path, monkeypatch):
    manager = _notes_manager(tmp_path, monkeypatch)
    assert ".eikocode" in str(manager.user_notes_path)
    assert str(manager.project_notes_path).startswith(str(tmp_path / "proj"))


def test_notes_turn_interval_triggers(tmp_path, monkeypatch):
    manager = _notes_manager(tmp_path, monkeypatch)
    manager._interval = 2
    session = Session()
    session.add_user("q")
    session.complete_assistant("a")
    spawned: list[int] = []
    manager._spawn = lambda s: spawned.append(1)  # 屏蔽真实异步线程

    manager.on_turn_end(session)  # 第 1 轮：未达标
    assert spawned == []
    manager.on_turn_end(session)  # 第 2 轮：触发
    assert spawned == [1]


def test_notes_written_to_correct_levels(tmp_path, monkeypatch):
    manager = _notes_manager(tmp_path, monkeypatch)

    class _NoteProvider:
        def __init__(self, tag):
            self.tag = tag

        def stream(self, messages, params: GenerateParams, tools=()):
            yield self.tag

    from eikocode.memory.notes import PROJECT_CATEGORIES, USER_CATEGORIES

    manager._update_one(
        _NoteProvider("## 用户偏好\n喜欢简洁\n## 纠正反馈\n（暂无）"),
        "m", manager.user_notes_path, USER_CATEGORIES, Session(),
    )
    manager._update_one(
        _NoteProvider("## 项目知识\n用 Python 3.13\n## 参考资料\n（暂无）"),
        "m", manager.project_notes_path, PROJECT_CATEGORIES, Session(),
    )
    assert "喜欢简洁" in manager.user_notes_path.read_text(encoding="utf-8")
    assert "Python 3.13" in manager.project_notes_path.read_text(encoding="utf-8")


def test_notes_failure_keeps_file_intact(tmp_path, monkeypatch):
    manager = _notes_manager(tmp_path, monkeypatch)
    manager.user_notes_path.parent.mkdir(parents=True, exist_ok=True)
    manager.user_notes_path.write_text("## 用户偏好\n原有内容", encoding="utf-8")

    class _BoomProvider:
        def stream(self, messages, params: GenerateParams, tools=()):
            raise EikoCodeError(ErrorKind.NETWORK, "网络炸了")
            yield  # pragma: no cover

    manager._update_one(_BoomProvider(), "m", manager.user_notes_path, ("用户偏好",), Session())
    assert "原有内容" in manager.user_notes_path.read_text(encoding="utf-8")


def test_notes_do_not_enter_session_history(tmp_path, monkeypatch):
    manager = _notes_manager(tmp_path, monkeypatch)
    session = Session()
    session.add_user("q")
    before = session.message_count
    manager._update(session)  # select_provider 未打桩 → 内部异常被静默吞掉
    assert session.message_count == before


# --------------------------------------------------------------------------- #
# 组 63：管理命令
# --------------------------------------------------------------------------- #
def test_list_archives_sorted_by_recent(tmp_path):
    for sid, active in [("a-1", "2026-09-01T10:00:00"), ("a-2", "2026-09-05T10:00:00")]:
        archiver = SessionArchiver(base_dir=tmp_path, session_id=sid)
        archiver._created_at = active
        # 直接写 meta（模拟不同活跃时间的两个存档）
        archiver.meta_path.write_text(
            json.dumps({
                "id": sid, "title": sid, "message_count": 1,
                "created_at": active, "last_active_at": active,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    metas = list_archives(base_dir=tmp_path)
    assert [m["id"] for m in metas] == ["a-2", "a-1"]


# --------------------------------------------------------------------------- #
# 组 64：运行时恢复接口
# --------------------------------------------------------------------------- #
def test_restore_session_replaces_history_and_stale_notice():
    runtime = AgentRuntime(
        config=SimpleNamespace(
            model="m", temperature=0.2, max_tokens=100, context_limit=100_000,
            auto_compact=False,
        ),
        session=Session(),
        registry=get_registry(),
        ask=lambda p: "y",
        ui=_FakeUi(),
        model="m",
    )
    restored = [_msg(Role.USER, "三天前的对话"), _msg(Role.ASSISTANT, "好的")]
    runtime.restore_session(
        restored, stale_notice="（提示：距上次对话已 3 天，期间项目可能已发生变化。）"
    )
    assert runtime.session.messages()[-3].content == "三天前的对话"
    assert "距上次对话已 3 天" in runtime.session.messages()[-1].content


def _msg(role, content, **kw):
    return Message(role=role, content=content, **kw)
