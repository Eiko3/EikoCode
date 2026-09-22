"""工具层与工具循环测试（离线，进程级）。

覆盖 checklist.md 第 10–17 组：
- 注册表按名查找 / 重复注册报错
- 执行器超时真杀进程（E10）
- 读取：内容 / 过大 / 二进制 / 缺失
- 搜索：glob / grep
- 写入：CRLF 保留
- 编辑：多段全成功 / 原子回滚 / 匹配不唯一 / 外部改动 / 不建新文件
- 权限：只读自动过 / 写需确认 / 危险需显式 yes
- 工具循环闭环（E7/E8/E9/E11）：请求→权限→执行→结果回写→续生成
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from eikocode import cli
from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.loop import _Ui
from eikocode.config import Config
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.providers.base import ToolCall
from eikocode.renderer import Renderer
from eikocode.session import Session
from eikocode.tools import execute_tool, get_registry
from eikocode.tools.base import PermissionLevel
from eikocode.tools.edit_file import EditFileTool
from eikocode.security.blacklist import contains_dangerous
from eikocode.tools.read_file import ReadFileTool
from eikocode.tools.search import GlobTool, GrepTool
from eikocode.tools.shell import ShellTool
from eikocode.tools.write_file import WriteFileTool


# --------------------------------------------------------------------------- #
# 测试用工具
# --------------------------------------------------------------------------- #
def _make_renderer():
    buffer = StringIO()
    console = Console(file=buffer, no_color=True, highlight=False, soft_wrap=True, width=100)
    return Renderer(False, console), buffer


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


class _FakeRenderer:
    def error(self, text):
        pass

    def notice(self, text):
        pass

    def info(self, text=""):
        pass


# --------------------------------------------------------------------------- #
# 注册表（第 10 组）
# --------------------------------------------------------------------------- #
def test_registry_finds_by_name():
    reg = get_registry()
    assert reg.get("ReadFile") is not None
    assert reg.get("EditFile") is not None
    assert reg.get("Shell") is not None
    assert reg.get("__nope__") is None


def test_registry_rejects_duplicate():
    from eikocode.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(ReadFileTool())
    with pytest.raises(EikoCodeError):
        reg.register(ReadFileTool())


def test_all_registered_tools_declare_permission():
    reg = get_registry()
    assert len(reg.names()) >= 5
    for tool in reg.all():
        assert tool.permission in (PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.EXECUTE)


# --------------------------------------------------------------------------- #
# 执行器超时真杀进程（第 11 组 / E10）
# --------------------------------------------------------------------------- #
def test_executor_timeout_kills_subprocess():
    tool = ShellTool()
    with tempfile.TemporaryDirectory() as d:
        pidfile = pathlib.Path(d) / "pid.txt"
        cmd = (
            f"$p=[System.Diagnostics.Process]::GetCurrentProcess().Id; "
            f"Set-Content -Path '{pidfile.as_posix()}' -Value $p; "
            f"Start-Sleep -Seconds 60"
        )
        with pytest.raises(EikoCodeError) as excinfo:
            execute_tool(tool, {"command": cmd}, timeout=1)
        assert excinfo.value.kind is ErrorKind.TIMEOUT

        pid = int(pidfile.read_text().strip())
        # Windows 上刚被杀的 pid 仍可能被 OpenProcess 打开，故用 tasklist 判定存活更稳
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert str(pid) not in listed.stdout, "超时后底层进程仍在运行"


def test_executor_error_categories_present_in_errors():
    # 错误层确实归一出这五类有限类别（第 11 组）
    for kind in (
        ErrorKind.TIMEOUT,
        ErrorKind.TOOL_PERMISSION_DENIED,
        ErrorKind.TOOL_TARGET_MISSING,
        ErrorKind.TOOL_COMMAND_FAILED,
        ErrorKind.TOOL_INVALID_ARG,
    ):
        assert kind in ErrorKind.__members__.values()


# --------------------------------------------------------------------------- #
# 读取与搜索（第 12 组）
# --------------------------------------------------------------------------- #
def test_read_returns_content(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello content", encoding="utf-8")
    out = ReadFileTool().execute({"path": str(f)})
    assert "hello content" in out


def test_read_rejects_too_large(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * (1024 * 1024 + 200))
    with pytest.raises(EikoCodeError) as excinfo:
        ReadFileTool().execute({"path": str(f)})
    assert "文件过大" in excinfo.value.user_message


def test_read_rejects_binary(tmp_path):
    f = tmp_path / "bin.bin"
    f.write_bytes(b"\x00\x01\x02abc")
    with pytest.raises(EikoCodeError) as excinfo:
        ReadFileTool().execute({"path": str(f)})
    assert "看起来是二进制文件" in excinfo.value.user_message


def test_read_rejects_missing(tmp_path):
    with pytest.raises(EikoCodeError) as excinfo:
        ReadFileTool().execute({"path": str(tmp_path / "nope.txt")})
    assert "文件不存在" in excinfo.value.user_message


def test_glob_returns_matches(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("")
    out = GlobTool().execute({"pattern": "*.py", "path": str(tmp_path)})
    assert "a.py" in out and "c.py" in out


def test_grep_returns_line_numbers(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("foo\nneedle123 bar\nbaz\n", encoding="utf-8")
    out = GrepTool().execute({"pattern": "needle123", "path": str(tmp_path)})
    # 格式应为 文件:行号:内容
    assert out.count(":") >= 2
    assert "needle123" in out


# --------------------------------------------------------------------------- #
# 写入与多段编辑（第 13 组）
# --------------------------------------------------------------------------- #
def test_write_preserves_crlf_for_existing_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep\r\ncrlf\r\n", encoding="utf-8", newline="")
    WriteFileTool().execute({"path": str(f), "content": "x\ny"})
    assert "\r\n" in f.read_text(encoding="utf-8", newline="")


def test_write_new_file_defaults_to_crlf(tmp_path):
    f = tmp_path / "new.txt"
    WriteFileTool().execute({"path": str(f), "content": "a\nb"})
    assert "\r\n" in f.read_text(encoding="utf-8", newline="")


def test_edit_multi_segment_applies_all(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("apple\r\nbanana\r\ncherry", encoding="utf-8", newline="")
    EditFileTool().execute(
        {
            "path": str(f),
            "edits": [
                {"old_string": "apple", "new_string": "APPLE"},
                {"old_string": "cherry", "new_string": "CHERRY"},
            ],
        }
    )
    assert f.read_text(encoding="utf-8", newline="") == "APPLE\r\nbanana\r\nCHERRY"


def test_edit_atomic_rollback_on_mismatch(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")
    with pytest.raises(EikoCodeError) as excinfo:
        EditFileTool().execute(
            {
                "path": str(f),
                "edits": [
                    {"old_string": "alpha", "new_string": "X"},
                    {"old_string": "zzz", "new_string": "Y"},
                ],
            }
        )
    assert "整次编辑未生效" in excinfo.value.user_message
    assert f.read_text(encoding="utf-8") == "alpha beta gamma"


def test_edit_rejects_duplicate_match(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("foo bar foo", encoding="utf-8")
    with pytest.raises(EikoCodeError) as excinfo:
        EditFileTool().execute(
            {"path": str(f), "edits": [{"old_string": "foo", "new_string": "baz"}]}
        )
    assert "匹配不唯一" in excinfo.value.user_message


def test_edit_rejects_missing_file(tmp_path):
    with pytest.raises(EikoCodeError) as excinfo:
        EditFileTool().execute(
            {"path": str(tmp_path / "nope.txt"), "edits": [{"old_string": "x", "new_string": "y"}]}
        )
    assert "文件不存在" in excinfo.value.user_message


def test_edit_rejects_external_change(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("alpha beta gamma", encoding="utf-8")

    class FlipPath(pathlib.Path):
        _n = 0

        def stat(self, *a, **k):
            FlipPath._n += 1
            st = super().stat(*a, **k)
            if FlipPath._n == 3:  # 写入前校验这次（is_dir/读取快照/写入前校验共三次 stat），假装外部改过 mtime
                seq = list(st)
                if len(seq) >= 12:
                    seq[11] = seq[11] + 1  # st_mtime_ns（Windows）
                elif len(seq) >= 9:
                    seq[8] = seq[8] + 1.0  # st_mtime（兜底）
                return os.stat_result(tuple(seq))
            return st

    monkeypatch.setattr("eikocode.tools.edit_file.Path", FlipPath)
    FlipPath._n = 0
    with pytest.raises(EikoCodeError) as excinfo:
        EditFileTool().execute(
            {"path": str(f), "edits": [{"old_string": "alpha", "new_string": "X"}]}
        )
    assert "文件已被外部改动" in excinfo.value.user_message
    assert f.read_text(encoding="utf-8") == "alpha beta gamma"


# --------------------------------------------------------------------------- #
# Shell（第 14 组）
# --------------------------------------------------------------------------- #
def test_shell_returns_output():
    out = ShellTool().execute({"command": "echo hello-eikocode"})
    assert "hello-eikocode" in out


def test_shell_nonzero_exit_normalized():
    with pytest.raises(EikoCodeError) as excinfo:
        ShellTool().execute({"command": "exit 2"})
    assert excinfo.value.kind is ErrorKind.TOOL_COMMAND_FAILED
    assert "2" in excinfo.value.user_message


def test_shell_cwd_persists_across_calls(tmp_path):
    tool = ShellTool()
    sub = tmp_path / "subdir"
    sub.mkdir()
    tool.execute({"command": f"Set-Location {sub.as_posix()}"})
    out = tool.execute({"command": "$PWD"})
    assert sub.name in out


# --------------------------------------------------------------------------- #
# 权限确认（第 15 组）——v5 起由安全决策流水接管，分类策略行为矩阵
# 迁移至 tests/test_security.py（组 38 档位矩阵 + 组 39 人在回路）。
# --------------------------------------------------------------------------- #
def test_contains_dangerous_patterns():
    for cmd in ("rm -rf x", "rmdir /s x", "del /f x", "format C:", "shutdown", "diskpart", "reg delete x", "takeown x", "icacls x /reset", ":(){ :|:& };", r"\\.\C:"):
        assert contains_dangerous(cmd), f"应判为危险：{cmd}"


# --------------------------------------------------------------------------- #
# 工具循环闭环（E7 / E8 / E9 / E11）
# --------------------------------------------------------------------------- #
def _run_tool_loop(provider, monkeypatch, confirm="y"):
    monkeypatch.setattr(
        "eikocode.agent.loop.select_provider", lambda config, model: provider
    )
    monkeypatch.setattr("builtins.input", lambda p: confirm)
    renderer, _ = _make_renderer()
    session = Session()
    runtime = AgentRuntime(
        config=_make_config(),
        session=session,
        registry=get_registry(),
        ask=lambda p: confirm,
        ui=_Ui(error=lambda t: None, notice=lambda t: None),
        model="gpt-4o",
    )
    list(runtime.run_turn("请操作", CancelToken()))
    return session, None


class _ReadLoopProvider:
    def __init__(self, path):
        self.path = str(path)
        self.supports_tools = True
        self.supports_temperature = True
        self.calls = 0

    def stream(self, messages, params, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(id="c1", name="ReadFile", arguments={"path": self.path})
        else:
            last = messages[-1]
            snippet = last.content[:15] if last.role.value == "user" else ""
            yield f"我已读取文件，前 15 字符为：{snippet}"


def test_e7_tool_call_loop(tmp_path, monkeypatch):
    f = tmp_path / "note.txt"
    f.write_text("alpha beta gamma delta", encoding="utf-8")
    session, code = _run_tool_loop(_ReadLoopProvider(f), monkeypatch)

    assert code is None
    tool_results = [m for m in session.messages() if m.tool_call_id is not None]
    assert tool_results, "工具结果未回写会话"
    # 续生成引用了文件内容（前 15 字符被回显），证明闭环成立
    assert "alpha beta" in session.messages()[-1].content


class _EditLoopProvider:
    def __init__(self, path):
        self.path = str(path)
        self.supports_tools = True
        self.supports_temperature = True
        self.calls = 0

    def stream(self, messages, params, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(
                id="c1",
                name="EditFile",
                arguments={"path": self.path, "edits": [{"old_string": "world", "new_string": "EikoCode"}]},
            )
        else:
            yield "已改写文件。"


def test_e8_edit_confirm_then_write(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("hello world", encoding="utf-8")
    session, code = _run_tool_loop(_EditLoopProvider(f), monkeypatch, confirm="y")

    assert code is None
    assert "EikoCode" in f.read_text(encoding="utf-8")


def test_e8_edit_denied_does_not_write(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("hello world", encoding="utf-8")
    session, code = _run_tool_loop(_EditLoopProvider(f), monkeypatch, confirm="n")

    assert code is None
    assert f.read_text(encoding="utf-8") == "hello world"
    assert any(
        "用户拒绝执行" in m.content
        for m in session.messages()
        if m.tool_call_id is not None
    )


class _BadEditLoopProvider:
    def __init__(self, path):
        self.path = str(path)
        self.supports_tools = True
        self.supports_temperature = True
        self.calls = 0

    def stream(self, messages, params, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(
                id="c1",
                name="EditFile",
                arguments={
                    "path": self.path,
                    "edits": [
                        {"old_string": "world", "new_string": "X"},
                        {"old_string": "ZZZ", "new_string": "Y"},
                    ],
                },
            )
        else:
            yield "完成。"


def test_e9_atomic_rollback_via_loop(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("hello world", encoding="utf-8")
    session, code = _run_tool_loop(_BadEditLoopProvider(f), monkeypatch, confirm="y")

    assert code is None
    assert f.read_text(encoding="utf-8") == "hello world"
    assert any(
        "整次编辑未生效" in m.content
        for m in session.messages()
        if m.tool_call_id is not None
    )


class _DangerLoopProvider:
    def __init__(self, cmd):
        self.cmd = cmd
        self.supports_tools = True
        self.supports_temperature = True
        self.calls = 0

    def stream(self, messages, params, tools=()):
        self.calls += 1
        if self.calls == 1:
            yield ToolCall(id="c1", name="Shell", arguments={"command": self.cmd})
        else:
            yield "done"


def test_e11_dangerous_denied(tmp_path, monkeypatch):
    d = tmp_path / "victim"
    d.mkdir()
    session, code = _run_tool_loop(
        _DangerLoopProvider(f"rm -rf {d.as_posix()}"), monkeypatch, confirm="no"
    )
    assert d.exists(), "危险命令被拦截，目录不应被删"
    assert any(
        "用户拒绝执行" in m.content
        for m in session.messages()
        if m.tool_call_id is not None
    )


def test_e11_dangerous_allowed_with_yes(tmp_path, monkeypatch):
    d = tmp_path / "victim"
    d.mkdir()
    session, code = _run_tool_loop(
        _DangerLoopProvider(f"Remove-Item -Recurse -Force {d.as_posix()}"),
        monkeypatch,
        confirm="yes",
    )
    assert not d.exists(), "显式 yes 后危险命令应被执行"
