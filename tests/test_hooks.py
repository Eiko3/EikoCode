"""v11 Hook 系统测试：格式解析、条件求值、动作执行器、执行控制、拦截循环、节点嵌入。

对应 checklist 组 82–88 与 E40（离线部分）。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.events import EventKind
from eikocode.agent.loop import _Ui
from eikocode.config import Config
from eikocode.hooks import HookEngine, parse_hooks
from eikocode.hooks.actions import execute_action, render_template, build_context
from eikocode.hooks.conditions import evaluate_conditions
from eikocode.hooks.engine import HookEngine as Engine
from eikocode.hooks.models import HookSpec, load_two_levels
from eikocode.providers.base import GenerateParams, ToolCall
from eikocode.session import Session
from eikocode.tools import ToolRegistry
from eikocode.tools.base import PermissionLevel, Tool


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #
def _config(**kw):
    base = dict(model="test-model", temperature=0.5, max_tokens=1024,
                context_limit=100_000, known_models=(),
                anthropic_api_key=None, openai_api_key="k", openai_base_url=None)
    base.update(kw)
    return Config(**base)


class _Provider:
    """桩供应商：plan 序列按调用次数分发。"""

    calls = 0
    plan = None
    requests = []

    def stream(self, messages, params: GenerateParams, tools=()):
        _Provider.requests.append(list(messages))
        plan = _Provider.plan
        _Provider.calls += 1
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


class _FakeTool(Tool):
    def __init__(self, name="ReadFile", level=PermissionLevel.READ):
        self.name = name
        self.permission = level
        self.ran = False

    def execute(self, args):
        self.ran = True
        return "tool-ok"


def _registry(*names):
    reg = ToolRegistry()
    for n in names:
        reg.register(_FakeTool(n, PermissionLevel.READ))
    return reg


def _runtime(reg, provider_cls=_Provider, hooks=None):
    notices: list = []
    ui = _Ui(error=lambda t: notices.append(("error", t)),
             notice=lambda t: notices.append(("notice", t)))
    runtime = AgentRuntime(
        config=_config(), session=Session(), registry=reg,
        ask=lambda p: "y", ui=ui, model="test-model", hooks=hooks,
    )
    return runtime, notices


def _engine(specs, notices, cwd="C:/proj"):
    return HookEngine(specs, lambda t: notices.append(t), cwd=cwd)


# --------------------------------------------------------------------------- #
# 组 82：格式与解析
# --------------------------------------------------------------------------- #
def _rule_text(event="tool_before", action_type="shell", extra_action="command: echo hi",
               conditions="", extra=""):
    cond = f"\n    conditions:{conditions}\n" if conditions is not None else ""
    return (
        "hooks:\n"
        f"  - event: {event}\n"
        f"{cond}"
        "    action:\n"
        f"      type: {action_type}\n"
        f"      {extra_action}\n"
        f"{extra}"
    )


def test_parse_ok_and_twelve_events():
    specs, errors = parse_hooks(_rule_text(), "测试")
    assert not errors and specs[0].event == "tool_before"
    assert specs[0].action["command"] == "echo hi"
    from eikocode.hooks.models import EVENTS  # noqa: E402
    for ev in EVENTS:
        s, e = parse_hooks(_rule_text(event=ev), "测试")
        assert not e, ev


def test_missing_event_or_action_located():
    specs, errors = parse_hooks(
        "hooks:\n  - action:\n      type: shell\n      command: x\n  - event: turn_start\n  - event: turn_end\n    action:\n      type: shell\n      command: y\n",
        "测试",
    )
    assert [s.event for s in specs] == ["turn_end"]
    assert any("第 1 条" in e and "event" in e for e in errors)
    assert any("第 2 条" in e and "action" in e for e in errors)


def test_unknown_event_and_action_type_rejected():
    _, e1 = parse_hooks(_rule_text(event="on_fire"), "测试")
    assert any("未知事件" in e for e in e1)
    _, e2 = parse_hooks(_rule_text(action_type="magic"), "测试")
    assert any("未知动作类型" in e for e in e2)


def test_action_required_fields():
    _, e = parse_hooks(
        "hooks:\n  - event: turn_start\n    action:\n      type: prompt\n      text: x\n  - event: turn_end\n    action:\n      type: http\n      method: POST\n",
        "测试",
    )
    assert not [x for x in e if "第 1 条" in x]
    assert any("url" in x for x in e)


# --------------------------------------------------------------------------- #
# 组 83：条件求值
# --------------------------------------------------------------------------- #
def _cond_ctx(**kw):
    base = {"tool": "WriteFile", "args": {"path": "a.log"}, "message": "hello world",
            "error": "boom", "cwd": "C:/proj", "event": "tool_before"}
    base.update(kw)
    return base


def test_condition_operators():
    assert evaluate_conditions({"tool": "WriteFile"}, _cond_ctx())  # 标量 = eq
    assert evaluate_conditions({"tool": {"not": "Shell"}}, _cond_ctx())
    assert evaluate_conditions({"args.path": {"glob": "*.log"}}, _cond_ctx())
    assert not evaluate_conditions({"args.path": {"glob": "*.txt"}}, _cond_ctx())
    assert evaluate_conditions({"message": {"regex": "w.rld"}}, _cond_ctx())
    assert not evaluate_conditions({"tool": {"eq": "Shell"}}, _cond_ctx())


def test_condition_match_all_any():
    cond = {"match": "all", "tool": "WriteFile", "args.path": {"glob": "*.log"}}
    assert evaluate_conditions(cond, _cond_ctx())
    assert not evaluate_conditions(cond, _cond_ctx(args={"path": "a.txt"}))
    cond_any = {"match": "any", "tool": "Shell", "args.path": {"glob": "*.log"}}
    assert evaluate_conditions(cond_any, _cond_ctx())


def test_condition_mixing_and_or_rejected():
    _, errors = parse_hooks(
        "hooks:\n  - event: tool_before\n    conditions:\n      match: all\n      and: true\n      tool: Shell\n    action:\n      type: shell\n      command: x\n",
        "测试",
    )
    assert any("混用" in e for e in errors)


def test_condition_undefined_field_is_empty():
    # 未定义字段取空串：glob "a*" 不命中空串；not 与空串比较按值判定
    assert not evaluate_conditions({"args.missing": {"glob": "a*"}}, _cond_ctx())
    assert evaluate_conditions({"args.missing": {"not": "x"}}, _cond_ctx())
    assert evaluate_conditions({"nope": {"not": "x"}}, _cond_ctx())  # 未定义字段 → 空串


def test_no_conditions_always_fires():
    assert evaluate_conditions(None, _cond_ctx())


# --------------------------------------------------------------------------- #
# 组 84：动作执行器
# --------------------------------------------------------------------------- #
def test_shell_action_success_and_failure():
    notices = []
    ok = execute_action({"type": "shell", "command": "echo hi"}, _cond_ctx(), 10, lambda t: notices.append(t))
    assert ok.success and "hi" in ok.output
    bad = execute_action({"type": "shell", "command": "exit 3"}, _cond_ctx(), 10, lambda t: notices.append(t))
    assert not bad.success


def test_shell_action_timeout():
    r = execute_action({"type": "shell", "command": "Start-Sleep -Seconds 5"}, _cond_ctx(), 1, lambda t: None)
    assert not r.success and "超时" in r.output


def test_prompt_action_injects():
    delivered = []
    r = execute_action({"type": "prompt", "text": "提醒 {tool}"}, _cond_ctx(), 10,
                       lambda t: None, inject=delivered.append)
    assert r.success and delivered == ["提醒 WriteFile"]


def test_subagent_action_placeholder():
    r = execute_action({"type": "subagent"}, _cond_ctx(), 10, lambda t: None)
    assert not r.success and "未实现" in r.output


def test_template_variables_undefined_to_empty():
    ctx = build_context("tool_before", tool="WriteFile", args={"path": "x.py"},
                        message="msg", error="err", cwd="C:/w")
    out = render_template("{event}/{tool}/{args.path}/{message}/{error}/{cwd}/{nope}/{args.nope}", ctx)
    assert out == "tool_before/WriteFile/x.py/msg/err/C:/w//"


def test_http_action_local_server():
    class H(BaseHTTPRequestHandler):
        status = 200

        def do_POST(self):
            # 读掉请求体，避免连接被过早关闭（WinError 10053）
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            body = json.dumps({"ok": True}).encode()
            self.send_response(H.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        ok = execute_action({"type": "http", "url": url}, _cond_ctx(), 5, lambda t: None)
        assert ok.success
        H.status = 500
        bad = execute_action({"type": "http", "url": url}, _cond_ctx(), 5, lambda t: None)
        assert not bad.success
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# 组 85：执行控制
# --------------------------------------------------------------------------- #
def _counting_action():
    """返回会被计数替换的动作容器。"""
    return {"type": "shell", "command": "echo run"}


def test_once_fires_single_time():
    fired = []
    spec = SimpleNamespace(event="turn_start", conditions=None, once=True, async_=False,
                           timeout=5, source="测试", index=1, action=None, is_interceptable=False,
                           action_type="shell")
    # 直接用真实 HookSpec（走 evaluate 分支），动作换成桩
    calls = []
    engine = _engine([], [])
    spec = HookSpec(event="turn_start", action={"type": "shell", "command": "echo x"}, once=True)
    # 用 monkeypatch 替换 execute_action 计数
    orig = "eikocode.hooks.engine.execute_action"
    from eikocode.hooks.actions import ActionResult
    import eikocode.hooks.engine as eng

    def fake_execute(action, context, timeout, notice, inject=None):
        calls.append(1)
        return ActionResult(True, "ok")

    eng.execute_action = fake_execute
    engine.specs = [spec]
    engine.observe("turn_start", message="a")
    engine.observe("turn_start", message="b")
    eng.execute_action = execute_action  # 还原
    assert len(calls) == 1


def test_async_does_not_block():
    engine = _engine([], [])
    spec = HookSpec(event="turn_start", action={"type": "shell", "command": "Start-Sleep -Seconds 3"},
                    async_=True)
    start = time.time()
    engine.specs = [spec]
    engine.observe("turn_start")
    elapsed = time.time() - start
    assert elapsed < 2  # 不阻塞主流程
    time.sleep(0.5)


def test_tool_before_ignores_async():
    """tool_before 上 async 被忽略：同步完成并参与拦截判定。"""
    engine = _engine([], [])
    spec = HookSpec(event="tool_before", action={"type": "shell", "command": "exit 1"}, async_=True)
    engine.specs = [spec]
    reason = engine.observe("tool_before", tool="Shell", args={"command": "x"})
    assert reason and "Hook 拦截" in reason


# --------------------------------------------------------------------------- #
# 组 86：拦截循环与错误隔离
# --------------------------------------------------------------------------- #
def test_tool_before_failure_intercepts():
    engine = _engine([], [])
    spec = HookSpec(event="tool_before",
                    action={"type": "shell", "command": "Write-Output '禁止写 log'; exit 1"},
                    source="项目级 hooks", index=1)
    engine.specs = [spec]
    reason = engine.observe("tool_before", tool="WriteFile", args={"path": "a.log"})
    assert reason and "禁止写 log" in reason and "项目级 hooks" in reason


def test_error_isolation_bad_hook_does_not_block():
    """非拦截事件上动作异常 → 只记通知，主流程照常。"""
    notices = []
    engine = _engine([], notices)
    # shell 命令必然失败但事件是 turn_start（非拦截事件）
    spec = HookSpec(event="turn_start", action={"type": "shell", "command": "exit 9"})
    engine.specs = [spec]
    reason = engine.observe("turn_start", message="x")
    assert reason is None  # 不拦截
    assert any("已忽略" in t for t in notices)


def test_interception_loop_in_run_turn(monkeypatch):
    """端到端离线闭环：第一次工具调用被 Hook 拦下并收到原因，第二次成功。"""
    reg = _registry("ReadFile")
    hook = HookSpec(
        event="tool_before",
        action={"type": "shell", "command": "if ('{args.path}' -like '*.log') { exit 1 } else { exit 0 }"},
        source="测试", index=1,
    )
    engine = HookEngine([hook], lambda t: None, cwd="C:/proj")
    runtime, notices = _runtime(reg, hooks=engine)

    # 轮 1：调用 ReadFile 读 a.log（被拦）→ 轮 2：读 b.txt（放行）→ 最终回复
    _Provider.plan = [
        [ToolCall(id="c1", name="ReadFile", arguments={"path": "a.log"})],
        [ToolCall(id="c2", name="ReadFile", arguments={"path": "b.txt"})],
        ["已改读 b.txt 完成"],
    ]
    events = list(runtime.run_turn("看日志", CancelToken()))

    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 2
    assert "Hook 拦截" in results[0].text  # 原因回写为工具结果
    assert results[1].text == "tool-ok"  # 调整后成功
    tool_results = [m for m in runtime.session.messages() if m.tool_call_id]
    assert any("Hook 拦截" in m.content for m in tool_results)


# --------------------------------------------------------------------------- #
# 组 87：加载与节点嵌入
# --------------------------------------------------------------------------- #
def test_two_levels_both_execute(tmp_path):
    user = tmp_path / "user_hooks.yaml"
    proj = tmp_path / "proj_hooks.yaml"

    # 两个文件各声明一条 session_start prompt 动作，验证都触发
    user.write_text(
        "hooks:\n  - event: session_start\n    action:\n      type: prompt\n      text: U\n",
        encoding="utf-8",
    )
    proj.write_text(
        "hooks:\n  - event: session_start\n    action:\n      type: prompt\n      text: P\n",
        encoding="utf-8",
    )
    specs, errors = load_two_levels(proj, user)
    assert not errors and len(specs) == 2
    assert [s.source for s in specs] == ["用户级 hooks", "项目级 hooks"]  # 用户级先

    notices = []
    engine = HookEngine(specs, lambda t: notices.append(t))
    engine.observe("session_start", message="x")
    assert sorted(engine.drain_injections()) == ["<system-reminder>P</system-reminder>", "<system-reminder>U</system-reminder>"]


def test_no_files_is_normal(tmp_path):
    specs, errors = load_two_levels(tmp_path / "nope.yaml", None)
    assert specs == [] and errors == []


def test_engine_nodes_observed_in_run_turn(monkeypatch):
    """八节点嵌入：桩引擎记录 run_turn 内各节点触发。"""
    reg = _registry("ReadFile")
    observed: list[str] = []

    class _StubEngine:
        def observe(self, event, **kw):
            observed.append(event)
            return None

        def drain_injections(self):
            return []

        def shutdown(self):
            observed.append("shutdown")

    runtime, _ = _runtime(reg, hooks=_StubEngine())
    _Provider.plan = [["你好"]]
    list(runtime.run_turn("问题", CancelToken()))

    assert observed.count("session_start") == 1
    assert observed.count("turn_start") >= 1
    assert observed.count("message_send") >= 1
    assert observed.count("message_receive") == 1
    assert observed.count("turn_end") == 1


def test_hook_prompt_injection_enters_next_request(monkeypatch):
    """prompt 动作的注入进入下一轮请求（对话通道）。"""
    reg = _registry("ReadFile")
    hook = HookSpec(event="message_receive",
                    action={"type": "prompt", "text": "【Hook 提醒】简洁回复"})
    notices = []
    ui = _Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t))
    runtime = AgentRuntime(config=_config(), session=Session(), registry=reg,
                           ask=lambda p: "y", ui=ui, model="m", hooks=None)
    engine = HookEngine([hook], lambda t: notices.append(t), inject=lambda t: runtime_hooks_inject(runtime, t))
    runtime.hooks = engine

    _Provider.plan = [["第一轮回复"], ["第二轮回复"]]
    list(runtime.run_turn("问1", CancelToken()))
    list(runtime.run_turn("问2", CancelToken()))

    second_req = _Provider.requests[-1]
    assert any("【Hook 提醒】简洁回复" in m.content for m in second_req)


def runtime_hooks_inject(runtime, text):
    runtime.hooks._deliver(text)
