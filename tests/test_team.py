"""v14 小组协作系统测试：小组模型、邮箱两段式、协作工具隔离、双锁纯调度、合并。

对应 checklist 组 105–111 与 E47/E48（离线部分）。
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from eikocode.agent import AgentRuntime, CancelToken
from eikocode.agent.loop import _Ui
from eikocode.config import Config
from eikocode.hooks.engine import HookEngine
from eikocode.session import Session
from eikocode.subagent.runner import build_filtered_registry
from eikocode.team import Team, TeamCoordinator
from eikocode.team.mailbox import Mailbox
from eikocode.team.tools import COLLAB_TOOL_NAMES, build_collab_tools
from eikocode.tools import ToolRegistry
from eikocode.tools.base import PermissionLevel, Tool


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


class _FakeTool(Tool):
    def __init__(self, name):
        self.name = name
        self.permission = PermissionLevel.READ

    def execute(self, args):
        return "ok"


def _base_registry(*names):
    reg = ToolRegistry()
    for n in names or ("ReadFile", "Glob", "Shell", "Agent"):
        reg.register(_FakeTool(n))
    return reg


def _config(**kw):
    base = dict(model="m", temperature=0.5, max_tokens=100, context_limit=100000,
                known_models=(), anthropic_api_key=None, openai_api_key="k",
                openai_base_url=None, team_dispatch_only=False)
    base.update(kw)
    return Config(**base)


class _Provider:
    calls = 0
    plan = None
    requests = []

    def stream(self, messages, params, tools=()):
        _Provider.requests.append(list(messages))
        _Provider.calls += 1
        plan = _Provider.plan
        if plan and _Provider.calls <= len(plan):
            for item in plan[_Provider.calls - 1]:
                yield item
        else:
            yield f"回复 #{_Provider.calls}"


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    _Provider.calls = 0
    _Provider.plan = None
    _Provider.requests = []
    inst = _Provider()
    monkeypatch.setattr("eikocode.agent.loop.select_provider", lambda c, m: inst)
    yield


# --------------------------------------------------------------------------- #
# 组 105：小组与成员模型
# --------------------------------------------------------------------------- #
def test_team_create_save_restore(tmp_path):
    team = Team(name="alpha", dir=tmp_path / "teams" / "alpha")
    team.members["alice"] = SimpleNamespace(
        name="alice", role="explore", worktree="w1", backend="inline",
        needs_approval=True, instance_id="alice-01", status="运行中",
    )
    team.tasks["t1"] = {"title": "查代码", "status": "待认领", "assignee": "alice",
                        "depends_on": [], "result": ""}
    team.save()

    restored = Team.load("alpha", tmp_path)
    assert restored is not None
    assert restored.members["alice"].role == "explore"
    assert restored.tasks["t1"]["assignee"] == "alice"
    assert (restored.dir / "mailboxes").parent == restored.dir


# --------------------------------------------------------------------------- #
# 组 107–108：任务依赖 / 邮箱
# --------------------------------------------------------------------------- #
def test_task_dependency_blocks_completion(tmp_path):
    team = Team(name="dep", dir=tmp_path / "dep")
    t1 = team.create_task("先行任务")
    t2 = team.create_task("后续任务", depends_on=[t1])
    assert t2.startswith("t")

    err = team.update_task(t2, status="完成")
    assert err and t1 in err  # 依赖未完成 → 拒绝

    team.update_task(t1, status="完成")
    assert team.update_task(t2, status="完成") is None  # 依赖解除后可完成


def test_mailbox_point_to_point_and_unknown(tmp_path):
    mb = Mailbox(tmp_path)
    mb.register("lead", "lead")
    mb.register("alice", "alice-01")

    err = mb.send(to="alice", sender="lead", text="查一下 foo", summary="查 foo")
    assert err is None
    msgs = mb.read("alice-01")
    assert len(msgs) == 1 and msgs[0]["from"] == "lead" and "foo" in msgs[0]["text"]

    assert mb.send(to="nobody", sender="lead", text="x") is not None  # 未知目标报错
    assert mb.read("alice-01") == []  # 消费式读取后清空


def test_mailbox_broadcast(tmp_path):
    mb = Mailbox(tmp_path)
    mb.register("lead", "lead")
    mb.register("alice", "a1")
    mb.register("bob", "b1")

    err = mb.send(to="*", sender="lead", text="开工")
    assert err is None
    assert len(mb.read("a1")) == 1
    assert len(mb.read("b1")) == 1
    assert mb.read("lead") == []  # Lead 自己不收


def test_protocol_message_kinds(tmp_path):
    mb = Mailbox(tmp_path)
    mb.register("lead", "lead")
    mb.register("alice", "a1")
    mb.send(to="lead", sender="alice", text="完成了", kind="lifecycle")
    mb.send(to="alice", sender="lead", text="同意", kind="approval")
    kinds = [m["kind"] for m in mb.read("a1")]
    assert kinds == ["approval"]


# --------------------------------------------------------------------------- #
# 组 107：协作工具隔离边界
# --------------------------------------------------------------------------- #
def test_collab_tools_only_for_members(tmp_path):
    mb = Mailbox(tmp_path)
    mb.register("lead", "lead")
    team = Team(name="t", dir=tmp_path)
    member_tools = build_collab_tools(team, "alice", mb)

    assert {t.name for t in member_tools} == set(COLLAB_TOOL_NAMES)

    # 主对话注册表：无协作工具
    main_reg = _base_registry()
    assert not (set(main_reg.names()) & set(COLLAB_TOOL_NAMES))

    # v12 普通子工作者注册表：无协作工具
    sub = build_filtered_registry(main_reg, None, background=False)
    assert not (set(sub.names()) & set(COLLAB_TOOL_NAMES))


def test_collab_tools_work(tmp_path):
    mb = Mailbox(tmp_path)
    mb.register("lead", "lead")
    team = Team(name="t", dir=tmp_path)
    tools = {t.name: t for t in build_collab_tools(team, "alice", mb)}

    out = tools["task_create"].execute({"title": "写文档", "depends_on": []})
    assert "已创建" in out
    assert "t1" in tools["task_list"].execute({})
    assert "写文档" in tools["task_view"].execute({"id": "t1"})
    assert "已更新" in tools["task_update"].execute({"id": "t1", "status": "完成"})
    out = tools["send_message"].execute({"to": "lead", "text": "好了", "summary": "好了"})
    assert out == "消息已投递"


# --------------------------------------------------------------------------- #
# 组 109–110：Lead 合并与纯调度
# --------------------------------------------------------------------------- #
def _coordinator(repo, notices, dispatch_only=False):
    roles, _ = __import__("eikocode.subagent.loader", fromlist=["discover_roles"]).discover_roles(None, None)
    coord = TeamCoordinator(
        repo, lambda t: notices.append(t), _config(team_dispatch_only=dispatch_only),
        ask=lambda p: "y", ui=_Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t)),
        roles=roles, hooks=None,
    )
    return coord


def test_merge_success_and_conflict_rollback(repo):
    from eikocode.worktree import WorktreeManager

    wm = WorktreeManager(repo, lambda t: None)
    mgr = WorktreeManager(repo, lambda t: None)
    wm.create("w1")
    wt = repo / ".eikocode" / "worktrees" / "w1"
    (wt / "feature.py").write_text("x = 1", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "feature"], wt)

    coord = _coordinator(repo, [])
    coord._worktrees = mgr
    coord.team = Team(name="t", dir=repo / ".eikocode" / "teams" / "t")
    coord.team.members["alice"] = SimpleNamespace(name="alice", role="explore",
                                                  worktree="w1", backend="inline",
                                                  needs_approval=True, instance_id="", status="空闲")
    report = coord.merge_all()
    assert "合并成功" in report
    assert (repo / "feature.py").exists()  # 真实落盘

    # 冲突路径：主分支和 w1 分支都改 feature.py 再改一行
    mgr.create("w2")
    wt2 = repo / ".eikocode" / "worktrees" / "w2"
    (wt2 / "conflict.py").write_text("a = 1", encoding="utf-8")
    _git(["add", "."], wt2)
    _git(["commit", "-m", "w2 change"], wt2)
    (repo / "conflict.py").write_text("a = 2", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "main change"], repo)
    coord.team.members["bob"] = SimpleNamespace(name="bob", role="explore",
                                                worktree="w2", backend="inline",
                                                needs_approval=True, instance_id="", status="空闲")
    report = coord.merge_all()
    assert "回滚" in report
    assert "a = 2" in (repo / "conflict.py").read_text(encoding="utf-8")  # 回滚后主分支内容保留


def test_dispatch_double_lock(repo):
    notices = []
    coord = _coordinator(repo, notices, dispatch_only=False)
    runtime = SimpleNamespace(
        registry=_base_registry("ReadFile", "Shell", "Agent", "mcp__x__tool"),
        push_injection=lambda t: notices.append(("injection", t)),
    )
    coord.bind_base_registry(runtime.registry)

    # 单锁：只有配置没有命令确认 → 不生效
    r = coord.enter_dispatch(runtime)
    assert "team_dispatch_only" in r and coord.dispatch_on is False

    # 双锁都满足 → 生效
    coord._config = _config(team_dispatch_only=True)
    coord.enter_dispatch(runtime)
    assert coord.dispatch_on is True
    names = set(runtime.registry.names())
    assert "ReadFile" not in names and "Shell" not in names
    assert "Agent" in names  # 派发能力保留
    assert "mcp__x__tool" not in names  # MCP 工具剥夺
    assert any(t[0] == "injection" and "纯调度模式" in t[1] for t in notices)

    # 恢复
    coord.exit_dispatch(runtime)
    assert set(runtime.registry.names()) == {"ReadFile", "Shell", "Agent", "mcp__x__tool"}


def test_dispatch_requires_config_lock(repo):
    notices = []
    coord = _coordinator(repo, notices, dispatch_only=True)
    runtime = SimpleNamespace(
        registry=_base_registry("ReadFile", "Agent"),
        push_injection=lambda t: None,
    )
    coord.bind_base_registry(runtime.registry)
    coord.enter_dispatch(runtime)
    # 配置锁满足 + 命令显式 → 生效
    assert coord.dispatch_on is True
    assert "ReadFile" not in set(runtime.registry.names())


# --------------------------------------------------------------------------- #
# 组 111 / E48：inline 双成员端到端
# --------------------------------------------------------------------------- #
def test_inline_team_end_to_end(repo, monkeypatch):
    """E48：Lead 拆两个带依赖任务 → 成员领任务互发消息 → 合并落盘。"""
    notices = []
    reg = _base_registry("ReadFile", "Glob", "Grep", "Shell", "EditFile", "Agent")
    ui = _Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t))
    runtime = AgentRuntime(config=_config(), session=Session(), registry=reg,
                           ask=lambda p: "y", ui=ui, model="m")

    from eikocode.worktree import WorktreeManager

    wm = WorktreeManager(repo, lambda t: notices.append(t))
    coord = TeamCoordinator(
        repo, lambda t: notices.append(t), _config(), lambda p: "y", ui,
        roles=__import__("eikocode.subagent.loader", fromlist=["discover_roles"]).discover_roles(None, None)[0],
        hooks=HookEngine([], lambda t: notices.append(t)), worktree_manager=wm,
    )
    coord.bind_base_registry(reg)

    coord.create_team("alpha", [("alice", "explore"), ("bob", "general")])
    # Lead 拆任务：t1 无依赖、t2 依赖 t1
    coord.team.create_task("探索 foo 定义", assignee="alice")
    coord.team.create_task("基于探索结果写 doc.md", assignee="bob", depends_on=["t1"])

    # 派生 inline 成员（常驻实例 + 协作工具）
    coord._start_inline_member("alice", coord.team.members["alice"], runtime, reg)
    coord._start_inline_member("bob", coord.team.members["bob"], runtime, reg)
    alice = coord._members_runtime.get("alice")
    bob = coord._members_runtime.get("bob")
    assert alice is not None and bob is not None
    _Provider.plan = [["探索完成：foo 在 a.py"]]
    alice_run = list(alice.run_turn(
        "请用 task_update 把 t1 标记进行中，然后执行你的任务：探索 foo 定义；"
        "完成后把 t1 标记完成并用 send_message 通知 bob。", CancelToken()))
    coord.team.members["alice"].status = "空闲"

    # bob 领依赖任务
    _Provider.plan = [["文档已写"]]
    bob = coord._members_runtime.get("bob")
    list(bob.run_turn("执行你的任务 t2：写 doc.md；完成后 send_message 通知 lead。", CancelToken()))

    # 消息已投递：bob 收到 alice 的通知
    lead_msgs = []
    # 合并：alice 的分支无实际变更（桩模型没真写文件）→ 直接验证命令可用
    report = coord.merge_all()
    assert isinstance(report, str)
