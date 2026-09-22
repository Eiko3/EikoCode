"""Git 工作树生命周期管理（v13）：创建 / 进入 / 退出 / 删除。

- 目录统一放 `<仓库>/.eikocode/worktrees/<名>`，经 `.git/info/exclude`
  本地隐藏（不改动用户的 .gitignore）。
- 快速恢复：目录已存在且 `.git` 元数据有效 → 纯文件系统复用，不调 git。
- 变更保护 fail-closed：检查本身失败也按「有变更」处理。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import branch_for, validate_name

WORKTREES_DIRNAME = "worktrees"

# 创建后复制的本地配置文件（提议默认，见 checklist 组 98）
DEFAULT_COPY_LOCAL_CONFIGS = ("settings.local.json",)


class WorktreeError(Exception):
    """工作树操作失败（含人类可读原因）。"""


def _git(args: list[str], cwd: Path) -> tuple[bool, str]:
    """执行只读 / 管理类 git 命令。返回 (成功, 合并输出)。"""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git 执行失败：{exc}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


class WorktreeManager:
    """单仓库的工作树生命周期管理。"""

    def __init__(self, repo_dir: Path, notice=lambda t: None) -> None:
        self.repo = Path(repo_dir).resolve()
        self.notice = notice
        self.base = self.repo / ".eikocode" / WORKTREES_DIRNAME
        self.current: str | None = None  # 当前进入的工作树名（None = 主目录）
        self._exclude_ensured = False

    # -- 基础 ---------------------------------------------------------------- #
    def is_repo(self) -> bool:
        return (self.repo / ".git").exists()

    def worktree_path(self, name: str) -> Path:
        return self.base / name

    def _ensure_hidden(self) -> None:
        """把 worktrees 目录写入 .git/info/exclude（本地排除，幂等）。"""
        if self._exclude_ensured:
            return
        exclude = self.repo / ".git" / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
            line = ".eikocode/worktrees/"
            if line not in existing.splitlines():
                exclude.write_text(existing.rstrip("\n") + ("\n" if existing.strip() else "") + line + "\n", encoding="utf-8")
            self._exclude_ensured = True
        except OSError:
            pass  # 隐藏失败不阻断，仅 status 可能显示目录

    # -- 变更检查（fail-closed） ---------------------------------------------- #
    def has_uncommitted(self, path: Path) -> bool:
        ok, out = _git(["status", "--porcelain"], cwd=path)
        if not ok:
            return True  # fail-closed
        return bool(out.strip())

    def has_unpushed(self, path: Path) -> bool:
        # 无远端的仓库没有「推送」概念，跳过该检查（否则一切提交都算未推送，
        # 干净目录永远删不掉）；有远端时按上游或全部本地分支判断。
        ok_remote, remotes = _git(["remote"], cwd=path)
        if not ok_remote or not remotes.strip():
            return False
        ok, out = _git(["log", "@{u}.."], cwd=path)
        if ok:
            return bool(out.strip())
        ok2, out2 = _git(["log", "--branches", "--not", "--remotes", "--oneline"], cwd=path)
        if not ok2:
            return True  # fail-closed
        return bool(out2.strip())

    def has_changes(self, path: Path) -> bool:
        return self.has_uncommitted(path) or self.has_unpushed(path)

    # -- 生命周期 -------------------------------------------------------------- #
    def create(self, name: str, config=None) -> str:
        err = validate_name(name)
        if err:
            return f"创建失败：{err}"
        if not self.is_repo():
            return "创建失败：当前目录不是 Git 仓库。"
        path = self.worktree_path(name)
        if (path / ".git").is_file() or (path / ".git").is_dir():
            return f"工作树 {name} 已存在，直接复用（快速恢复）。"  # 快速恢复：不调 git

        self._ensure_hidden()
        self.base.mkdir(parents=True, exist_ok=True)
        ok, out = _git(["worktree", "add", "-b", branch_for(name), str(path)], cwd=self.repo)
        if not ok:
            # 分支已存在等情形：不带 -b 重试一次
            ok2, out2 = _git(["worktree", "add", str(path)], cwd=self.repo)
            if not ok2:
                return f"创建失败：{(out + out2).strip()[:300]}"

        self._init_environment(name, path, config)
        self.notice(f"已创建工作树 {name}（分支 {branch_for(name)}）")
        return f"工作树 {name} 已创建：{path}"

    def _init_environment(self, name: str, path: Path, config=None) -> None:
        """创建后环境初始化（尽力而为，失败仅提示）。"""
        copy_configs = DEFAULT_COPY_LOCAL_CONFIGS
        symlink_dirs: list[str] = []
        copy_files: list[str] = []
        if config is not None:
            copy_configs = list(getattr(config, "worktree_copy_local_configs", None) or copy_configs)
            symlink_dirs = list(getattr(config, "worktree_symlink_dirs", None) or [])
            copy_files = list(getattr(config, "worktree_copy_files", None) or [])

        # 1) 复制本地配置
        for fname in copy_configs:
            src = self.repo / fname
            if src.is_file():
                try:
                    shutil.copy2(src, path / fname)
                except OSError as exc:
                    self.notice(f"工作树 {name}：复制 {fname} 失败（{exc}）")

        # 2) git hooks 路径对齐
        ok, hooks_path = _git(["config", "--get", "core.hooksPath"], cwd=self.repo)
        if ok and hooks_path.strip():
            _git(["config", "core.hooksPath", hooks_path.strip()], cwd=path)

        # 3) 大型依赖目录软链接（失败降级为跳过）
        for d in symlink_dirs:
            src = self.repo / d
            dst = path / d
            if not src.is_dir() or dst.exists():
                continue
            try:
                dst.symlink_to(src, target_is_directory=True)
            except OSError as exc:
                self.notice(f"工作树 {name}：软链接 {d} 失败（{exc}），已跳过")

        # 4) 被忽略但需要的文件复制（best-effort）
        for f in copy_files:
            src = self.repo / f
            if not src.is_file():
                self.notice(f"工作树 {name}：copy_files 源 {f} 不存在，已跳过")
                continue
            try:
                dst = path / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError as exc:
                self.notice(f"工作树 {name}：复制 {f} 失败（{exc}）")

    def enter(self, name: str, runtime=None) -> str:
        err = validate_name(name)
        if err:
            return f"进入失败：{err}"
        path = self.worktree_path(name)
        if not (path / ".git").exists():
            return f"进入失败：工作树 {name} 不存在（先 create）。"
        self.current = name
        if runtime is not None:
            reset_for_directory(runtime, path)
        self.notice(f"已进入工作树 {name}（{path}）")
        return f"已进入工作树 {name}：{path}"

    def exit(self, runtime=None) -> str:
        if self.current is None:
            return "当前不在任何工作树中。"
        name = self.current
        self.current = None
        if runtime is not None:
            reset_for_directory(runtime, self.repo)
        self.notice(f"已退出工作树 {name}，回到主目录")
        return f"已退出工作树 {name}。"

    def delete(self, name: str, force: bool = False, runtime=None) -> str:
        err = validate_name(name)
        if err:
            return f"删除失败：{err}"
        path = self.worktree_path(name)
        if not path.exists():
            return f"删除失败：工作树 {name} 不存在。"
        if self.current == name:
            self.exit(runtime)
        if not force and self.has_changes(path):
            return (
                f"拒绝删除 {name}：存在未提交修改或未推送提交。"
                "请先提交推送，或使用 /worktree delete {name} force 显式丢弃。"
            )
        ok, out = _git(["worktree", "remove", "--force", str(path)], cwd=self.repo)
        if not ok:
            shutil.rmtree(path, ignore_errors=True)  # 目录兜底清理
        _git(["branch", "-D", branch_for(name)], cwd=self.repo)  # 分支可能已被随 worktree 删除
        return f"工作树 {name} 已删除。"

    # -- 查询 ------------------------------------------------------------------ #
    def list(self) -> list[dict]:
        items = []
        if not self.base.is_dir():
            return items
        for path in sorted(self.base.iterdir()):
            if not path.is_dir():
                continue
            name = path.relative_to(self.base).as_posix()
            items.append({
                "name": name,
                "path": str(path),
                "current": self.current == name,
                "changes": self.has_changes(path),
            })
        return items

    def status_lines(self) -> list[str]:
        if not self.is_repo():
            return ["当前目录不是 Git 仓库，工作树能力不可用。"]
        items = self.list()
        if not items:
            return ["没有工作树。用 /worktree create <名字> 创建。"]
        lines = []
        for item in items:
            mark = "（当前）" if item["current"] else ""
            dirty = "有变更" if item["changes"] else "干净"
            lines.append(f"{item['name']}{mark}  [{dirty}]  {item['path']}")
        return lines

    # -- 持久化（--resume 用） --------------------------------------------------- #
    @property
    def state_file(self) -> Path:
        return self.base / ".current"

    def save_state(self) -> None:
        if not self.base.is_dir():
            return
        try:
            self.state_file.write_text(self.current or "", encoding="utf-8")
        except OSError:
            pass

    def restore_state(self, runtime=None) -> str | None:
        """--resume：读取上次进入的工作树名并重新进入。"""
        try:
            name = self.state_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not name:
            return None
        if not (self.worktree_path(name) / ".git").exists():
            return None
        self.enter(name, runtime)
        return name


def reset_for_directory(runtime, new_cwd: Path) -> None:
    """切换工作目录时的缓存清理（spec.md §3 能力 105）。

    - 环境快照重置：切换后首个请求的环境消息为新目录（v4 追加式，不改历史）
    - 指令缓存：按新目录重新加载项目指令
    - memory 文件缓存：笔记路径按新目录重载（NotesManager.rebase）
    - 文件内容缓存：当前工具层无读缓存，接口保留（未来加缓存时在此清空）
    """
    if runtime is None:
        return
    runtime._env_first = None
    runtime._env_last = None
    runtime._session_started = False  # 新目录视为新的一段会话环境
    try:
        from ..memory import load_instructions

        text, loaded = load_instructions(new_cwd, Path.home() / ".eikocode")
        if text:
            runtime._instructions = text
    except Exception:
        pass
    if getattr(runtime, "notes", None) is not None and hasattr(runtime.notes, "rebase"):
        try:
            runtime.notes.rebase(new_cwd)
        except Exception:
            pass
