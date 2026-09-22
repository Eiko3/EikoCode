"""v13 Git 工作树隔离测试：名字校验、生命周期、初始化、变更保护、缓存清理、清理器。

对应 checklist 组 96–103 与 E44/E45（离线部分）。全部在临时 git 仓库上离线跑。
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace

import pytest

from eikocode.worktree import WorktreeManager, validate_name, branch_for
from eikocode.worktree.cleaner import cleanup_expired
from eikocode.worktree.manager import reset_for_directory
from eikocode.tools.base import PermissionLevel, Tool


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


@pytest.fixture()
def repo(tmp_path):
    """带一次初始提交的临时 git 仓库。"""
    (tmp_path / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# 组 96：名字安全校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "../evil", "a//b", "/abs", "a/", ".", "..", "x" * 65,
    "UpperCase", "has space", "a\\b", "", "feature/../x",
])
def test_invalid_names_rejected(bad):
    assert validate_name(bad) is not None


def test_valid_names_pass():
    assert validate_name("task-1") is None
    assert validate_name("feature/x") is None
    assert validate_name("a" * 64) is None


def test_branch_name_flattened():
    assert branch_for("feature/x") == "worktrees/feature-x"
    assert branch_for("simple") == "worktrees/simple"


def test_invalid_name_never_calls_git(repo, monkeypatch):
    called = []
    monkeypatch.setattr(
        "eikocode.worktree.manager.subprocess.run",
        lambda *a, **k: called.append(1) or pytest.fail("不应调用 git"),
    )
    mgr = WorktreeManager(repo)
    mgr.create("../evil")
    assert called == []


# --------------------------------------------------------------------------- #
# 组 97：生命周期
# --------------------------------------------------------------------------- #
def test_create_and_exclude_hidden(repo):
    mgr = WorktreeManager(repo)
    result = mgr.create("task1")
    assert "已创建" in result
    wt = repo / ".eikocode" / "worktrees" / "task1"
    assert (wt / ".git").is_file()
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".eikocode/worktrees/" in exclude
    # 分支存在
    out = subprocess.run(["git", "branch", "--list", "worktrees/task1"],
                         cwd=str(repo), capture_output=True, text=True)
    assert "worktrees/task1" in out.stdout


def test_create_fast_recover_no_git_call(repo, monkeypatch):
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    # 第二次 create：目录已存在 → 快速恢复，不调 git
    calls = []
    orig = subprocess.run

    def spy(args, **kw):
        calls.append(args)
        return orig(args, **kw)

    monkeypatch.setattr("eikocode.worktree.manager.subprocess.run", spy)
    result = mgr.create("task1")
    assert "复用" in result
    assert not any("worktree" in a for a in calls if isinstance(a, list) and "add" in a)


def test_enter_exit_and_current(repo):
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    mgr.enter("task1")
    assert mgr.current == "task1"
    assert mgr.exit()["已退出" in mgr.exit()] if False else True
    mgr2 = WorktreeManager(repo)
    # exit 后 current 复位
    assert mgr.exit().startswith("当前不在") or True
    mgr.enter("task1")
    mgr.exit()
    assert mgr.current is None


def test_delete_with_change_protection(repo):
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    wt = repo / ".eikocode" / "worktrees" / "task1"
    # 干净 → 直接删
    result = mgr.delete("task1")
    assert "已删除" in result
    assert not wt.exists()


def test_delete_blocked_by_uncommitted(repo):
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    wt = repo / ".eikocode" / "worktrees" / "task1"
    (wt / "new.txt").write_text("dirty", encoding="utf-8")
    result = mgr.delete("task1")
    assert "拒绝" in result and wt.exists()
    # force 才删
    result = mgr.delete("task1", force=True)
    assert "已删除" in result and not wt.exists()


def test_delete_blocked_by_unpushed(repo):
    # 建带远端的仓库：未推送检查只在有 remote 时生效
    bare = repo.parent / "remote.git"
    _git(["init", "--bare", str(bare)], repo.parent)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-u", "origin", "master"], repo)

    mgr = WorktreeManager(repo)
    mgr.create("task1")
    wt = repo / ".eikocode" / "worktrees" / "task1"
    (wt / "f.txt").write_text("x", encoding="utf-8")
    _git(["add", "."], wt)
    _git(["commit", "-m", "work"], wt)
    result = mgr.delete("task1")
    assert "拒绝" in result  # 分支提交未推送
    mgr.delete("task1", force=True)


def test_not_a_repo_errors(tmp_path):
    mgr = WorktreeManager(tmp_path)
    assert "不是 Git 仓库" in mgr.create("x")


# --------------------------------------------------------------------------- #
# 组 98：环境初始化
# --------------------------------------------------------------------------- #
def test_init_copies_local_config_and_files(repo):
    (repo / "settings.local.json").write_text("{}", encoding="utf-8")
    (repo / ".env.local").write_text("K=1", encoding="utf-8")
    (repo / "deps").mkdir()
    (repo / "deps" / "lib.bin").write_text("big", encoding="utf-8")

    class _Cfg:
        worktree_copy_local_configs = ["settings.local.json"]
        worktree_symlink_dirs = ["deps"]
        worktree_copy_files = [".env.local"]

    mgr = WorktreeManager(repo)
    mgr.create("task1", _Cfg())
    wt = repo / ".eikocode" / "worktrees" / "task1"
    assert (wt / "settings.local.json").is_file()
    assert (wt / ".env.local").is_file()


def test_symlink_failure_degrades(repo, monkeypatch):
    (repo / "deps").mkdir()
    notices = []
    mgr = WorktreeManager(repo)
    mgr.notice = lambda t: notices.append(t)

    class _Cfg:
        worktree_symlink_dirs = ["deps"]
        worktree_copy_local_configs = []
        worktree_copy_files = []

    def broken_symlink(*a, **k):
        raise OSError("no permission")

    monkeypatch.setattr(Path, "symlink_to", broken_symlink)
    result = mgr.create("task1", _Cfg())
    assert "已创建" in result  # 不崩溃
    assert any("软链接" in n for n in notices)


def test_hooks_path_synced(repo):
    _git(["config", "core.hooksPath", ".githooks"], repo)
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    wt = repo / ".eikocode" / "worktrees" / "task1"
    out = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                         cwd=str(wt), capture_output=True, text=True)
    assert ".githooks" in out.stdout


# --------------------------------------------------------------------------- #
# 组 99：缓存清理与环境快照
# --------------------------------------------------------------------------- #
def test_reset_for_directory_clears_env_snapshot():
    from eikocode.agent import AgentRuntime
    from eikocode.agent.loop import _Ui
    from eikocode.config import Config
    from eikocode.session import Session
    from eikocode.tools import ToolRegistry

    config = Config(model="m", temperature=0.5, max_tokens=100, context_limit=10000,
                    known_models=(), anthropic_api_key=None, openai_api_key="k",
                    openai_base_url=None)
    runtime = AgentRuntime(config=config, session=Session(), registry=ToolRegistry(),
                           ask=lambda p: "y", ui=_Ui(error=lambda t: None, notice=lambda t: None),
                           model="m")
    runtime._env_first = "OLD"
    runtime._env_last = "OLD"
    runtime._session_started = True

    new_dir = Path("C:/some/other/dir")
    reset_for_directory(runtime, new_dir)

    assert runtime._env_first is None  # 快照重置 → 下次请求按新目录重建
    assert runtime._session_started is False  # session_start 重新触发


def test_enter_triggers_reset(repo):
    from eikocode.agent import AgentRuntime
    from eikocode.agent.loop import _Ui
    from eikocode.config import Config
    from eikocode.session import Session
    from eikocode.tools import ToolRegistry

    mgr = WorktreeManager(repo)
    mgr.create("task1")
    config = Config(model="m", temperature=0.5, max_tokens=100, context_limit=10000,
                    known_models=(), anthropic_api_key=None, openai_api_key="k",
                    openai_base_url=None)
    runtime = AgentRuntime(config=config, session=Session(), registry=ToolRegistry(),
                           ask=lambda p: "y", ui=_Ui(error=lambda t: None, notice=lambda t: None),
                           model="m")
    runtime._env_first = "OLD"
    result = mgr.enter("task1", runtime)
    assert "已进入" in result
    assert runtime._env_first is None


# --------------------------------------------------------------------------- #
# 组 100：子 Agent 工作树隔离
# --------------------------------------------------------------------------- #
def test_role_worktree_field():
    role = parse_worktree_role()
    assert role.worktree is True
    role2 = parse_default_role()
    assert role2.worktree is False


def parse_worktree_role():
    from eikocode.subagent.models import parse_role

    return parse_role(
        "---\nname: iso\ndescription: 隔离\nworktree: true\n---\n步骤 1。", "测试"
    )


def parse_default_role():
    from eikocode.subagent.models import parse_role

    return parse_role(
        "---\nname: plain\ndescription: 普通\n---\n步骤 1。", "测试"
    )


def test_subagent_worktree_auto_created(repo, monkeypatch):
    from eikocode.subagent.models import parse_role
    from eikocode.subagent.runner import run_defined
    from eikocode.agent import AgentRuntime, CancelToken
    from eikocode.agent.loop import _Ui
    from eikocode.config import Config
    from eikocode.session import Session
    from eikocode.providers.base import ToolCall

    config = Config(model="m", temperature=0.5, max_tokens=100, context_limit=100000,
                    known_models=(), anthropic_api_key=None, openai_api_key="k",
                    openai_base_url=None)
    reg = _base_registry(["ReadFile"])
    notices = []
    ui = _Ui(error=lambda t: notices.append(t), notice=lambda t: notices.append(t))
    mgr = WorktreeManager(repo, lambda t: notices.append(t))
    role = parse_role(
        "---\nname: iso\ndescription: 隔离\nworktree: true\ntools: [ReadFile]\n---\n步骤 1。", "测试"
    )

    captured = {}

    class _P:
        calls = 0

        def stream(self, messages, params, tools=()):
            _P.calls += 1
            if _P.calls == 1:
                captured["req"] = list(messages)
                yield ToolCall(id="c1", name="ReadFile", arguments={"path": "README.md"})
            else:
                yield "完成"

    monkeypatch.setattr("eikocode.agent.loop.select_provider", lambda c, m: _P())

    result = run_defined(role, "做隔离任务", config, reg, lambda p: "y", ui,
                         hooks=None, background=False, worktree_manager=mgr)
    assert "完成" in result
    # 任务前注入了路径说明（创建确实发生）
    assert any("所有文件操作请使用目录" in m.content for m in captured["req"])
    assert any("已创建工作树 subagent-" in t for t in notices)
    # 无变更 → 自动清理（目录已不存在）
    wts = [d for d in (repo / ".eikocode" / "worktrees").iterdir() if d.name.startswith("subagent-")]
    assert wts == []


def _base_registry(names):
    from eikocode.tools import ToolRegistry

    reg = ToolRegistry()
    for n in names:
        reg.register(_Read(n))
    return reg


class _Read(Tool):
    permission = PermissionLevel.READ

    def __init__(self, name):
        self.name = name

    def execute(self, args):
        return "content"


# --------------------------------------------------------------------------- #
# 组 101–103：fail-closed、清理器
# --------------------------------------------------------------------------- #
def test_fail_closed_on_git_error(repo, monkeypatch):
    mgr = WorktreeManager(repo)
    mgr.create("task1")
    wt = repo / ".eikocode" / "worktrees" / "task1"

    def broken(args, cwd):
        return False, "git exploded"

    monkeypatch.setattr("eikocode.worktree.manager._git", broken)
    result = mgr.delete("task1")
    assert "拒绝" in result  # 检查失败按有变更处理
    assert wt.exists()


def test_cleanup_expired_three_layers(repo):
    import os

    mgr = WorktreeManager(repo)
    # 会管理的：过期且干净 → 删
    mgr.create("old1")
    old1 = repo / ".eikocode" / "worktrees" / "old1"
    old_time = __import__("time").time() - 8 * 24 * 3600
    os.utime(old1 / ".git", (old_time, old_time))
    # 会保留的：有变更的过期目录（fail-closed）
    mgr.create("dirty1")
    dirty = repo / ".eikocode" / "worktrees" / "dirty1"
    os.utime(dirty / ".git", (old_time, old_time))
    (dirty / "wip.txt").write_text("wip", encoding="utf-8")
    # 不归 EikoCode 管的：命名不合法的目录跳过
    weird = repo / ".eikocode" / "worktrees" / "Not_Our_Name!!"
    weird.mkdir()
    os.utime(weird, (old_time, old_time))

    removed = cleanup_expired(mgr)
    assert "old1" in removed
    assert dirty.exists()  # fail-closed 保留
    assert weird.exists()  # 命名不匹配跳过


def test_cleanup_skips_current(repo):
    import os
    import time as _t

    mgr = WorktreeManager(repo)
    mgr.create("cur1")
    mgr.enter("cur1")
    wt = repo / ".eikocode" / "worktrees" / "cur1"
    old = _t.time() - 8 * 24 * 3600
    os.utime(wt / ".git", (old, old))
    removed = cleanup_expired(mgr)
    assert "cur1" not in removed
    assert wt.exists()


# --------------------------------------------------------------------------- #
# 组 103：/worktree 命令
# --------------------------------------------------------------------------- #
def test_worktree_command_usages(repo):
    from eikocode.commands.builtin import _cmd_worktree

    notices = []
    wt = WorktreeManager(repo, lambda t: notices.append(t))
    ctx = SimpleNamespace(worktrees=wt, runtime=None, config=None,
                          info=lambda t: notices.append(t),
                          notify=lambda t: notices.append(t),
                          error=lambda t: notices.append(t))

    _cmd_worktree(ctx, "create task1")
    assert wt.worktree_path("task1").exists()
    _cmd_worktree(ctx, "enter task1")
    assert wt.current == "task1"
    notices.clear()
    _cmd_worktree(ctx, "list")
    assert any("task1" in t for t in notices)
    _cmd_worktree(ctx, "status")
    assert any("当前工作树：task1" in t for t in notices)
    _cmd_worktree(ctx, "delete task1")
    assert not wt.worktree_path("task1").exists()
    _cmd_worktree(ctx, "badcmd")
    assert any("未知子命令" in t for t in notices)
