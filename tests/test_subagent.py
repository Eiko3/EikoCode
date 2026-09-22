"""v12 子工作者系统测试：角色加载、工具过滤、双模式执行、后台管理、命令。

对应 checklist 组 89–95 与 E43（离线部分）。
"""

from types import SimpleNamespace

import pytest

from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.events import EventKind
from eikocode.agent.loop import _Ui
from eikocode.config import Config
from eikocode.hooks.engine import HookEngine
from eikocode.hooks.engine import HookEngine
from eikocode.hooks.models import HookSpec
from eikocode.session import Session
from eikocode.subagent import AgentTool, parse_role
from eikocode.subagent.loader import discover_roles
from eikocode.subagent.manager import SubagentManager, TaskManager
from eikocode.subagent.runner import build_filtered_registry, run_defined, run_fork
from eikocode.providers.base import GenerateParams, ToolCall
from eikocode.tools import ToolRegistry
from eikocode.tools.base import PermissionLevel, Tool


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #
class _FakeTool(Tool):
    def __init__(self, name, level=PermissionLevel.READ):
        self.name = name
        self.permission = level
        self.ran = False

    def execute(self, args):
        self.ran = True
        return "tool-ok"


FULL_NAMES = ("ReadFile", "Glob", "Grep", "Shell", "EditFile")


def _base_registry(*names):
    reg = ToolRegistry()
    levels = {"ReadFile": PermissionLevel.READ, "Glob": PermissionLevel.READ,
              "Grep": PermissionLevel.READ, "Shell": PermissionLevel.EXECUTE,
              "EditFile": PermissionLevel.WRITE}
    for n in names or FULL_NAMES:
        reg.register(_FakeTool(n, levels.get(n, PermissionLevel.READ)))
    return reg


def _role_text(name="demo", desc="演示角色", tools=None, deny=None, model="",
               max_turns=None, mode="", sop="角色步骤 1。"):
    lines = ["---", f"name: {name}", f"description: {desc}"]
    if tools:
        lines.append(f"tools: [{', '.join(tools)}]")
    if deny:
        lines.append(f"deny_tools: [{', '.join(deny)}]")
    if model:
        lines.append(f"model: {model}")
    if max_turns:
        lines.append(f"max_turns: {max_turns}")
    if mode:
        lines.append(f"permission_mode: {mode}")
    lines += ["---", "", sop, ""]
    return "\n".join(lines)


class _Provider:
    calls = 0
    plan = None
    requests = []

    def stream(self, messages, params: GenerateParams, tools=()):
        _Provider.requests.append(list(messages))
        _Provider.calls += 1
        plan = _Provider.plan
        if plan and _Provider.calls <= len(plan):
            for item in plan[_Provider.calls - 1]:
                yield item
        else:
            yield f"最终回复 #{_Provider.calls}"


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    _Provider.calls = 0
    _Provider.plan = None
    _Provider.requests = []
    inst = _Provider()
    monkeypatch.setattr("eikocode.agent.loop.select_provider", lambda c, m: inst)
    yield


def _config(**kw):
    base = dict(model="test-model", temperature=0.5, max_tokens=1024,
                context_limit=100_000, known_models=(),
                anthropic_api_key=None, openai_api_key="k", openai_base_url=None)
    base.update(kw)
    return Config(**base)


def _notices():
    return []


# --------------------------------------------------------------------------- #
# 组 89：角色模型与三级加载
# --------------------------------------------------------------------------- #
def test_parse_role_full():
    role = parse_role(_role_text(tools=["ReadFile", "Grep"], deny=["Shell"],
                                 model="m2", max_turns="8", mode="strict"), "测试")
    assert role.name == "demo" and role.tools == ("ReadFile", "Grep")
    assert role.deny_tools == ("Shell",) and role.model == "m2"
    assert role.max_turns == 8 and role.permission_mode == "strict"
    assert role.sop == "角色步骤 1。"


def test_parse_role_failures():
    from eikocode.hooks.yaml_mini import YamlParseError
    for bad in ("no-frontmatter", "---\nname: demo\n---\n", "---\nname: BAD\n---\nx"):
        with pytest.raises(YamlParseError):
            parse_role(bad, "坏文件")


def test_three_levels_override(tmp_path):
    (tmp_path / "demo.md").write_text(_role_text(name="demo", desc="项目级"), encoding="utf-8")
    specs, errors = discover_roles(tmp_path, None)
    assert not errors
    demo = next(r for r in specs if r.name == "demo")
    assert demo.description == "项目级" and "项目级" in demo.source
    names = {r.name for r in specs}
    assert {"explore", "plan", "general"} <= names


def test_broken_role_skipped_and_named(tmp_path):
    (tmp_path / "a.md").write_text(_role_text(name="a"), encoding="utf-8")
    (tmp_path / "bad.md").write_text("---\nno_name\n---\n正文", encoding="utf-8")
    (tmp_path / "b.md").write_text(_role_text(name="b"), encoding="utf-8")
    specs, errors = discover_roles(tmp_path, None)
    assert {"a", "b"} <= {r.name for r in specs}
    assert any("bad.md" in e for e in errors)


def test_verify_role_needs_switch(tmp_path):
    _, e1 = discover_roles(None, None)
    specs_off = discover_roles(None, None)[0]
    assert "verify" not in {r.name for r in specs_off}
    specs_on = discover_roles(None, None, verify_enabled=True)[0]
    assert "verify" in {r.name for r in specs_on}
    assert e1 == []


# --------------------------------------------------------------------------- #
# 组 90：工具过滤多层防线
# --------------------------------------------------------------------------- #
def test_global_filter_excludes_agent_tool():
    reg = _base_registry()
    reg.register(_FakeTool("Agent", PermissionLevel.READ))  # 启动工具
    sub = build_filtered_registry(reg, None, background=False)
    assert sub.get("Agent") is None  # 任何模式都排除
    assert sub.get("ReadFile") is not None


def test_role_deny_and_allowlist():
    reg = _base_registry()
    role = parse_role(_role_text(tools=["ReadFile"], deny=["Glob"]), "测试")
    sub = build_filtered_registry(reg, role, background=False)
    assert sub.get("ReadFile") is not None
    assert sub.get("Glob") is None  # 黑名单
    assert sub.get("Shell") is None  # 白名单外


def test_background_readonly_intersection():
    reg = _base_registry()
    role = parse_role(_role_text(tools=["ReadFile", "Shell"]), "测试")
    sub = build_filtered_registry(reg, role, background=True)
    assert sub.get("ReadFile") is not None
    assert sub.get("Shell") is None  # 后台只读：执行类被移除


def test_tool_list_stable():
    reg = _base_registry()
    before = set(reg.names())
    # 加载角色 / 构建子注册表不影响主注册表
    build_filtered_registry(reg, None, background=False)
    assert set(reg.names()) == before


# --------------------------------------------------------------------------- #
# 组 91–92：定义式与 Fork 执行
# --------------------------------------------------------------------------- #
def _parent_setup():
    reg = _base_registry(*FULL_NAMES)
    session = Session()
    session.add_user("父对话用户消息")
    session.complete_assistant("父对话回复")
    notices = []
    ui = _Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t))
    runtime = AgentRuntime(config=_config(), session=session, registry=reg,
                           ask=lambda p: "y", ui=ui, model="test-model")
    return reg, session, runtime, notices


def test_defined_mode_runs_to_completion():
    reg, session, runtime, notices = _parent_setup()
    role = parse_role(_role_text(tools=["ReadFile"]), "测试")
    _Provider.plan = [
        [ToolCall(id="c1", name="ReadFile", arguments={"path": "x.py"})],
        ["探索完成：结论 X"],
    ]
    result = run_defined(role, "查找 foo", _config(), reg,
                         runtime._ask, runtime._ui, background=False)
    assert result == "探索完成：结论 X"
    # 父会话不受影响
    assert session.message_count == 2
    # 子请求：首条 = 角色指令（instructions），首次请求末条 = 任务
    req0 = _Provider.requests[0]
    assert "【角色：demo】" in req0[0].content
    assert "【任务】" in req0[-1].content


def test_max_turns_override_limits():
    reg = _base_registry()
    role = parse_role(_role_text(max_turns="2"), "测试")
    # 桩每轮都发工具调用 → 永不终止 → max_turns=2 触发超限失败
    _Provider.plan = [
        [ToolCall(id=f"c{i}", name="ReadFile", arguments={"path": "x"})] for i in range(10)
    ]
    result = run_defined(role, "任务", _config(), reg, lambda p: "y",
                         _Ui(error=lambda t: None, notice=lambda t: None))
    assert "失败" in result


def test_hook_intercepts_in_subagent():
    reg = _base_registry("ReadFile")
    hook = HookSpec(event="tool_before",
                    action={"type": "shell", "command": "Write-Output 'blocked-by-hook'; exit 1"},
                    source="测试", index=1)
    notices = []
    hook_engine = HookEngine([hook], lambda t: notices.append(t))
    runtime = AgentRuntime(config=_config(), session=Session(), registry=reg,
                           ask=lambda p: "y",
                           ui=_Ui(error=lambda t: None, notice=lambda t: notices.append(t)),
                           model="m", hooks=hook_engine)
    role = parse_role(_role_text(), "测试")
    _Provider.plan = [
        [ToolCall(id="c1", name="ReadFile", arguments={"path": "x.py"})],
        ["工具被拦后直接回复"],
    ]
    result = run_defined(role, "任务", _config(), reg, runtime._ask, runtime._ui,
                         hooks=hook_engine, background=False)
    assert any("Hook 执行 shell" in t for t in notices)  # 共享 Hook 引擎已触发


def test_fork_inherits_history_and_prefix(monkeypatch):
    reg, session, runtime, notices = _parent_setup()
    parent_req_before = None
    _Provider.plan = [
        ["Fork 结果：完成"],
    ]
    result = run_fork("完成任务", _config(), reg, session.messages(),
                      runtime.instructions, None, runtime._ask, runtime._ui,
                      background=True)
    assert result == "Fork 结果：完成"
    req = _Provider.requests[-1]
    # 继承父历史：请求含父消息
    assert any("父对话用户消息" in m.content for m in req)
    # 覆盖性指令逐字出现
    assert any("不得再调用启动子工作者的工具" in m.content for m in req)
    assert any("300 字以内" in m.content for m in req)


def test_fork_prefix_matches_parent_request():
    """Fork 请求的历史部分与父请求逐字节一致（缓存前缀可命中）。"""
    reg, session, runtime, notices = _parent_setup()
    # 父请求：走一轮普通对话
    _Provider.plan = [["父回复"]]
    list(runtime.run_turn("父任务", CancelToken()))
    parent_req = list(_Provider.requests[-1])
    # Fork 请求：继承同一历史
    _Provider.plan = [["Fork 完成"]]
    run_fork("子任务", _config(), reg, session.messages(), runtime.instructions,
             None, runtime._ask, runtime._ui, background=True)
    fork_req = list(_Provider.requests[-1])
    # Fork 请求以父请求为前缀（差异是父回复完成后多出的历史 + 任务消息）
    for a, b in zip(parent_req, fork_req):
        assert a.content == b.content and a.role == b.role, (a.content, b.content)
    assert len(fork_req) >= len(parent_req)


# --------------------------------------------------------------------------- #
# 组 93–94：后台管理与命令
# --------------------------------------------------------------------------- #
def _sub_setup():
    reg = _base_registry(*FULL_NAMES)
    notices = []
    task_manager = TaskManager(lambda t: notices.append(t))
    roles, errors = discover_roles(None, None)
    sub = SubagentManager(roles, task_manager, lambda t: notices.append(t))
    ui = _Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t))
    runtime = AgentRuntime(config=_config(), session=Session(), registry=reg,
                           ask=lambda p: "y", ui=ui, model="test-model")
    sub.bind(runtime)
    ctx = SimpleNamespace(tasks=task_manager,
                          info=lambda t: notices.append(t),
                          notify=lambda t: notices.append(t),
                          error=lambda t: notices.append(t))
    return sub, task_manager, ctx, runtime, notices


def test_explicit_background_runs_and_notifies():
    sub, tm, ctx, runtime, notices = _sub_setup()
    _Provider.plan = [["后台任务完成文本"]]
    role_arg, task_arg = "explore", "找一下 foo"
    result = sub.start_task(role_arg, task_arg, background=True)
    assert "已转后台" in result
    # 等线程完成
    task = tm.list_tasks()[0]
    task.thread.join(timeout=10)
    assert task.status == "完成" and "后台任务完成文本" in task.result
    # 完成通知进入主对话注入队列（下一轮请求生效）
    assert len(runtime._extra_injections) == 1
    assert "已" in runtime._extra_injections[0]


def test_background_permission_denied_recorded():
    sub, tm, ctx, runtime, notices = _sub_setup()
    reg = _base_registry(*FULL_NAMES)
    role = next(r for r in sub._roles.values() if r.name == "general")
    _Provider.plan = [["报告：写文件被拒绝"]]
    result = run_defined(role, "写个文件", _config(), reg,
                         lambda p: pytest.fail("后台不应询问用户"),
                         runtime._ui, background=True)
    assert "报告" in result  # 后台不询问、模型收到拒绝后继续


def test_tasks_command_three_usages():
    sub, tm, ctx, runtime, notices = _sub_setup()
    from eikocode.commands.builtin import _cmd_tasks

    # 空态
    _cmd_tasks(ctx, "")
    assert any("没有后台任务" in t for t in notices)
    # 提交一个快任务
    _Provider.plan = [["ok"]]
    sub.start_task("explore", "任务", background=True)
    tm.list_tasks()[0].thread.join(timeout=10)
    # 列表
    notices.clear()
    _cmd_tasks(ctx, "")
    assert any("explore" in t and "完成" in t for t in notices)
    # 详情
    notices.clear()
    tid = tm.list_tasks()[0].id
    _cmd_tasks(ctx, tid)
    assert any("任务 " + tid in t for t in notices)
    # kill 不存在的
    notices.clear()
    _cmd_tasks(ctx, "kill deadbeef")
    assert any("无法终止" in t for t in notices)


def test_agent_tool_returns_immediate_for_background():
    reg = _base_registry(*FULL_NAMES)
    sub, tm, ctx, runtime, notices = _sub_setup()
    tool = AgentTool(sub.start_task)
    _Provider.plan = [["后台完成"]]
    out = tool.execute({"role": "explore", "task": "探索", "background": True})
    assert "已转后台" in out
    tm.list_tasks()[0].thread.join(timeout=10)
    assert tm.list_tasks()[0].result == "后台完成"
    out2 = tool.execute({"task": ""})
    assert "缺少 task" in out2
