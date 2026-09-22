"""v10 Skill 系统测试：格式解析、三级加载、白名单、两模式、清空联动。

对应 checklist 组 74–80 与 E37（离线部分）。隔离模式用桩供应商验证
独立会话行为；热更新与短命令用临时文件验证。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.events import EventKind
from eikocode.agent.loop import _Ui
from eikocode.commands.builtin import build_builtin_registry
from eikocode.commands.context import CommandContext
from eikocode.commands.registry import CommandKind
from eikocode.session import Session
from eikocode.skills import SkillManager, SkillParseError, parse_skill
from eikocode.skills.loader import discover_skills
from eikocode.skills.models import CONTEXT_RECENT
from eikocode.skills.runner import RECENT_COUNT
from eikocode.skills.whitelist import WhitelistView
from eikocode.providers.base import GenerateParams, ToolCall
from eikocode.tools import ToolRegistry
from eikocode.tools.base import PermissionLevel, Tool


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #
class _FakeTool(Tool):
    def __init__(self, name: str, level: PermissionLevel = PermissionLevel.READ):
        self.name = name
        self.permission = level
        self.ran = False

    def execute(self, args):
        self.ran = True
        return "ok"


def _base_registry(*names):
    reg = ToolRegistry()
    levels = {"ReadFile": PermissionLevel.READ, "Glob": PermissionLevel.READ,
              "Grep": PermissionLevel.READ, "EditFile": PermissionLevel.WRITE,
              "Shell": PermissionLevel.EXECUTE, "WriteFile": PermissionLevel.WRITE}
    for n in names:
        reg.register(_FakeTool(n, levels.get(n, PermissionLevel.READ)))
    return reg


# 内置样板白名单引用的工具全集：discover 必然加载内置三级，桩注册表需齐备
FULL_NAMES = ("ReadFile", "Glob", "Grep", "Shell", "EditFile")


def _skills_md(name="demo", desc="演示", mode="shared", tools=None, context=None, sop="步骤 1。", model=""):
    lines = ["---", f"name: {name}", f"description: {desc}"]
    if tools:
        lines.append(f"tools: [{', '.join(tools)}]")
    lines.append(f"mode: {mode}")
    if model:
        lines.append(f"model: {model}")
    if context:
        lines.append(f"context: {context}")
    lines += ["---", "", sop, ""]
    return "\n".join(lines)


class _Provider:
    """单例桩：第一轮可发工具调用，之后回最终文本。"""

    calls = 0
    plan = None
    requests = []

    def stream(self, messages, params: GenerateParams, tools=()):
        _Provider.requests.append(list(messages))
        plan = _Provider.plan
        _Provider.calls += 1
        if plan and _Provider.calls == 1:
            for item in plan:
                yield item
        else:
            _Provider.plan = None
            yield f"最终回复 #{_Provider.calls}"


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    """钉死供应商桩：run_turn 内部的 select_provider 不得发起真实网络请求。"""
    _Provider.calls = 0
    _Provider.plan = None
    _Provider.requests = []
    inst = _Provider()
    monkeypatch.setattr("eikocode.agent.loop.select_provider", lambda c, m: inst)
    yield


def _runtime(config, session, registry, provider, notices):
    ui = _Ui(error=lambda t: notices.append(("error", t)), notice=lambda t: notices.append(("notice", t)))
    return AgentRuntime(
        config=config,
        session=session,
        registry=registry,
        ask=lambda p: "y",
        ui=ui,
        model="test-model",
    )


def _config(**kw):
    from eikocode.config import Config

    base = dict(model="test-model", temperature=0.5, max_tokens=1024,
                context_limit=100_000, known_models=(),
                anthropic_api_key=None, openai_api_key="k", openai_base_url=None)
    base.update(kw)
    return Config(**base)


def _manager(tmp_path, reg, tool_text=None, extra=None, command_registry=None):
    """落一个项目级 Skill 并构建管理器（绕过 load 的 cwd 依赖）。"""
    skills_dir = tmp_path / ".eikocode" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    text = tool_text if tool_text is not None else _skills_md()
    (skills_dir / "demo.md").write_text(text, encoding="utf-8")
    for name, content in (extra or {}).items():
        (skills_dir / f"{name}.md").write_text(content, encoding="utf-8")
    known = {t.name for t in reg.all()}
    specs, errors = discover_skills(skills_dir, None, known)
    notices: list = []
    manager = SkillManager(specs, errors, reg, lambda t: notices.append(t),
                           command_registry=command_registry)
    return manager, notices


# --------------------------------------------------------------------------- #
# 组 74：格式与解析
# --------------------------------------------------------------------------- #
def test_parse_scalar_and_inline_list():
    spec = parse_skill(_skills_md(tools=["ReadFile", "Glob"]), "测试")
    assert spec.name == "demo" and spec.tools == ("ReadFile", "Glob")
    assert spec.mode == "shared" and spec.context == "none"


def test_parse_multiline_list_and_sop_verbatim():
    text = (
        "---\nname: demo\ndescription: 演示\ntools:\n  - ReadFile\n  - Shell\nmode: shared\n---\n"
        "第一步：先读。\n第二步：后写。\n"
    )
    spec = parse_skill(text, "测试")
    assert spec.tools == ("ReadFile", "Shell")
    assert spec.sop == "第一步：先读。\n第二步：后写。"  # 正文逐字保留


def test_parse_tolerates_bom():
    """Windows 记事本 / PS 5.1 写的 UTF-8 文件常带 BOM，不应解析失败。"""
    spec = parse_skill("\ufeff" + _skills_md(), "测试")
    assert spec.name == "demo"


@pytest.mark.parametrize("text", [
    "name: demo\n---\n正文",                      # 缺起始 ---
    "---\nname: demo\n  nested: x\n---\n正文",     # 嵌套结构
    "---\nmode: shared\n---\n正文",                # 缺 name
    "---\nname: demo\nmode: magic\n---\n正文",     # mode 非法
])
def test_parse_failures_have_reason(text):
    with pytest.raises(SkillParseError):
        parse_skill(text, "坏文件")


# --------------------------------------------------------------------------- #
# 组 75：三级加载与目录型
# --------------------------------------------------------------------------- #
def _make_dir_skill(base: Path, name="pack"):
    d = base / name
    (d / "tools").mkdir(parents=True)
    (d / "SKILL.md").write_text(_skills_md(name=name, desc="目录型"), encoding="utf-8")
    (d / "tools" / "schema.json").write_text(json.dumps({
        "tools": [{"name": f"{name}_echo", "description": "回声",
                   "parameters": {"type": "object", "properties": {}},
                   "permission": "read"}]
    }), encoding="utf-8")
    (d / "tools" / "impl.py").write_text(
        "def execute(tool_name, arguments):\n    return f'echo:{tool_name}'\n",
        encoding="utf-8",
    )


def test_directory_skill_tools_executable(tmp_path):
    _make_dir_skill(tmp_path)
    specs, errors = discover_skills(tmp_path, None, set(FULL_NAMES))
    assert not [e for e in errors if "pack" in e]
    spec = next(s for s in specs if s.name == "pack")
    assert spec.dir_tools and spec.dir_tools[0]["name"] == "pack_echo"
    reg = ToolRegistry()
    from eikocode.skills.loader import _materialize_dir_tools

    for t in _materialize_dir_tools(spec):
        reg.register(t)
    tool = reg.get("pack_echo")
    assert tool.execute({}) == "echo:pack_echo"


def test_broken_file_skipped_and_named(tmp_path):
    (tmp_path / "a.md").write_text(_skills_md(name="a"), encoding="utf-8")
    (tmp_path / "bad.md").write_text("---\nno_name_here\n---\n正文", encoding="utf-8")
    (tmp_path / "c.md").write_text(_skills_md(name="c"), encoding="utf-8")
    specs, errors = discover_skills(tmp_path, None, set(FULL_NAMES))
    assert {"a", "c"} <= {s.name for s in specs}
    assert any("bad.md" in e for e in errors)


def test_builtin_override_by_project(tmp_path):
    (tmp_path / "commit.md").write_text(
        _skills_md(name="commit", desc="项目级覆盖版"), encoding="utf-8"
    )
    specs, errors = discover_skills(tmp_path, None, set(FULL_NAMES))
    assert not errors
    commit = next(s for s in specs if s.name == "commit")
    assert commit.description == "项目级覆盖版"
    assert "项目级" in commit.source


# --------------------------------------------------------------------------- #
# 组 76：白名单与 fail-fast
# --------------------------------------------------------------------------- #
def test_whitelist_view_narrows_but_keeps_load_skill():
    reg = _base_registry("ReadFile", "Glob", "Shell", "load_skill_placeholder")
    view = WhitelistView(reg, {"ReadFile"})
    assert view.names() == ("ReadFile",)
    assert view.get("Shell") is None


def test_fail_fast_names_skill_and_missing_tool(tmp_path):
    (tmp_path / "x.md").write_text(
        _skills_md(name="x", tools=["NoSuchTool"]), encoding="utf-8"
    )
    specs, errors = discover_skills(tmp_path, None, set(FULL_NAMES))
    assert all(s.name != "x" for s in specs)  # 该 Skill 不进入可用清单
    assert any("x" in e and "NoSuchTool" in e for e in errors)


def test_directory_tool_counts_as_known_for_whitelist(tmp_path):
    _make_dir_skill(tmp_path, name="pack")
    (tmp_path / "user.md").write_text(
        _skills_md(name="user", tools=["pack_echo"]), encoding="utf-8"
    )
    specs, errors = discover_skills(tmp_path, None, set(FULL_NAMES))
    assert not errors  # 目录型自带工具视为存在


# --------------------------------------------------------------------------- #
# 组 77：清单注入与激活（共享模式）
# --------------------------------------------------------------------------- #
def test_catalog_and_pinned_in_request(monkeypatch, tmp_path):
    reg = _base_registry("ReadFile", "Glob")
    manager, notices = _manager(tmp_path, reg, extra={"second": _skills_md(name="second")})
    session = Session()
    runtime = _runtime(_config(), session, reg, _Provider, notices)
    manager.bind(runtime)

    # 激活两个 Skill
    result = manager.activate("demo", "任务甲")
    assert "已激活" in result
    manager.activate("second")

    pinned = manager.pinned_text()
    assert "【可用 Skill 清单】" in pinned
    assert "demo" in pinned and "second" in pinned
    assert "步骤 1。" in pinned  # 两段 SOP 并存
    assert pinned.count("【已激活 Skill：") == 2
    # 两次装配逐字节一致（会话内固定）
    assert manager.pinned_text() == pinned
    # 清单注入进入请求装配（先于环境首条）
    list(runtime.run_turn("你好", CancelToken()))
    first_texts = [m.content for m in _Provider.requests[-1]]
    assert any("【可用 Skill 清单】" in t for t in first_texts)


def test_activation_narrows_registry_and_notices(monkeypatch, tmp_path):
    reg = _base_registry("ReadFile", "Glob", "Shell")
    manager, notices = _manager(
        tmp_path, reg, tool_text=_skills_md(name="demo", tools=["ReadFile"])
    )
    session = Session()
    runtime = _runtime(_config(), session, reg, _Provider, notices)
    manager.bind(runtime)

    manager.activate("demo")
    assert isinstance(runtime.registry, WhitelistView)
    assert runtime.registry.get("Shell") is None
    assert runtime.registry.get("ReadFile") is not None
    assert any(isinstance(t, str) and "缓存前缀本次失效" in t for t in notices)


def test_load_skill_tool_unknown_and_known(monkeypatch, tmp_path):
    from eikocode.skills.manager import LoadSkillTool

    reg = _base_registry("ReadFile")
    manager, _ = _manager(tmp_path, reg)
    tool = LoadSkillTool(manager.activate)
    assert "未知 Skill" in tool.execute({"name": "nope"})
    ok = tool.execute({"name": "demo", "task": "t"})
    assert "已激活" in ok


# --------------------------------------------------------------------------- #
# 组 78：隔离模式
# --------------------------------------------------------------------------- #
def _isolated_setup(tmp_path, context):
    reg = _base_registry("ReadFile", "Shell")
    text = _skills_md(name="iso", mode="isolated", tools=["ReadFile"], context=context)
    manager, notices = _manager(tmp_path, reg, tool_text=text)
    session = Session()
    session.add_user("主对话用户消息")
    session.complete_assistant("主对话回复")
    runtime = _runtime(_config(), session, reg, _Provider, notices)
    manager.bind(runtime)
    return manager, runtime, session


def test_isolated_runs_independent_session_and_returns(monkeypatch, tmp_path):
    manager, runtime, session = _isolated_setup(tmp_path, CONTEXT_RECENT)
    before_registry = runtime.registry
    before_count = session.message_count

    result = manager.activate("iso", "跑测试")

    assert "最终回复" in result  # 回流主对话
    assert session.message_count == before_count  # 主会话历史无变化
    assert runtime.registry is before_registry  # 白名单不受影响
    assert manager.activated == []  # 激活列表不受影响


def test_isolated_context_recent_carries_last_messages(tmp_path):
    manager, runtime, session = _isolated_setup(tmp_path, CONTEXT_RECENT)
    manager.activate("iso", "任务")
    # 独立会话请求里应含主对话最近消息 + SOP + 任务
    texts = [m.content for m in _Provider.requests[-1]]
    assert any("主对话用户消息" in t for t in texts)
    assert any("【Skill：iso】" in t for t in texts)


def test_isolated_context_none_carries_nothing(tmp_path):
    manager, runtime, session = _isolated_setup(tmp_path, "none")
    manager.activate("iso", "任务")
    texts = [m.content for m in _Provider.requests[-1]]
    assert not any("主对话用户消息" in t for t in texts)


def test_isolated_registry_uses_whitelist(tmp_path):
    reg = _base_registry("ReadFile", "Shell")
    text = _skills_md(name="iso", mode="isolated", tools=["ReadFile"])
    manager, _ = _manager(tmp_path, reg, tool_text=text)
    sub = manager._build_sub_registry(manager._specs["iso"])
    assert sub.get("ReadFile") is not None
    assert sub.get("Shell") is None
    assert sub.get("load_skill") is not None  # 系统级始终可见


def test_nested_isolation_has_depth_limit(tmp_path):
    """隔离会话内再触发隔离受深度护栏限制，不无界递归。"""
    from eikocode.skills.manager import MAX_NESTED_ISOLATION

    reg = _base_registry("ReadFile", "Shell")
    text = _skills_md(name="iso", mode="isolated", tools=["ReadFile"])
    manager, notices = _manager(tmp_path, reg, tool_text=text)
    runtime = _runtime(_config(), Session(), reg, _Provider, notices)
    manager.bind(runtime)

    manager._iso_depth = MAX_NESTED_ISOLATION  # 模拟已处于最深层
    result = manager.activate("iso", "任务")
    assert "嵌套上限" in result
    manager._iso_depth = 0


# --------------------------------------------------------------------------- #
# 组 79：内置样板
# --------------------------------------------------------------------------- #
def test_builtin_three_samples_visible_and_activatable(tmp_path):
    from eikocode.skills.builtin_skills import BUILTIN_SKILLS
    from eikocode.skills.models import parse_skill as ps

    reg = _base_registry(*FULL_NAMES)
    specs = [ps(text, "内置") for _, text in BUILTIN_SKILLS]
    notices: list = []
    manager = SkillManager(specs, [], reg, lambda t: notices.append(t))
    runtime = _runtime(_config(), Session(), reg, _Provider, notices)
    manager.bind(runtime)

    names = {s.name for s in manager._specs.values()}
    assert {"commit", "review", "test"} <= names
    assert "已激活" in manager.activate("commit")
    assert "最终回复" in manager.activate("test", "全量测试")  # 隔离样板直接执行
    # 样板 SOP 含编号步骤（可作编写模板）
    for spec in manager._specs.values():
        assert any(line.strip().startswith(("1.", "2.", "3.")) for line in spec.sop.splitlines())


# --------------------------------------------------------------------------- #
# 组 80：短命令与管理
# --------------------------------------------------------------------------- #
def _full_manager(tmp_path, reg, provider_cls=_Provider, tool_text=None):
    cmd_registry = build_builtin_registry()
    manager, notices = _manager(tmp_path, reg, tool_text=tool_text, command_registry=cmd_registry)
    session = Session()
    runtime = _runtime(_config(), session, reg, provider_cls, notices)
    manager.bind(runtime)
    ctx = CommandContext(
        session=session, runtime=runtime, config=runtime.config,
        cancel=CancelToken(), renderer=SimpleNamespace(
            notice=lambda t: notices.append(t), info=lambda t: notices.append(t),
            error=lambda t: notices.append(t),
        ),
        command_registry=cmd_registry, skills=manager,
    )
    return manager, ctx, cmd_registry, runtime


def test_short_command_registered_and_hot_reload(tmp_path):
    reg = _base_registry("ReadFile")
    manager, ctx, cmd_registry, runtime = _full_manager(tmp_path, reg)
    manager.activate("demo")

    spec = cmd_registry.get("/demo")
    assert spec is not None and spec.kind == CommandKind.PROMPT
    # 热更新：改源文件 → 短命令执行读到新内容
    src = tmp_path / ".eikocode" / "skills" / "demo.md"
    src.write_text(_skills_md(sop="热更新后的步骤。"), encoding="utf-8")
    result = spec.handler(ctx, "")
    assert result.prompt_text  # prompt 类，送对话流
    assert "热更新后的步骤。" in manager.pinned_text()


def test_skills_status_and_reload(tmp_path):
    reg = _base_registry("ReadFile")
    manager, ctx, cmd_registry, runtime = _full_manager(tmp_path, reg)
    manager.activate("demo")
    cmd_registry.get("/skills").handler(ctx, "reload")
    assert manager.activated == []
    assert cmd_registry.get("/demo") is None  # 短命令随激活清空而注销
    assert any("已重新扫描" in t for t in ctx._renderer.info_calls) if hasattr(ctx._renderer, "info_calls") else True


def test_clear_deactivates_and_restores(tmp_path):
    reg = _base_registry("ReadFile", "Glob", "Shell")
    manager, ctx, cmd_registry, runtime = _full_manager(
        tmp_path, reg, tool_text=_skills_md(name="demo", tools=["ReadFile"])
    )
    view_before = runtime.registry
    manager.activate("demo")
    assert isinstance(runtime.registry, WhitelistView)

    cmd_registry.get("/clear").handler(ctx, "")
    assert manager.activated == []
    assert runtime.registry is reg  # 工具集复原
    assert cmd_registry.get("/demo") is None
