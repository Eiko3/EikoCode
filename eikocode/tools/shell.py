"""命令执行工具。执行权限。默认 Windows PowerShell。

同一会话内工作目录跨调用延续（先 `cd` 再 `pwd` 显示同一目录）。执行期受统一
超时约束，超时时由执行器调用 `kill()` 杀掉整个进程树，不留孤儿。非零退出码
归一为 `TOOL_COMMAND_FAILED`，文案带退出码数字，供模型自我修正。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from .base import PermissionLevel, Tool

_CD_RE = re.compile(r"^\s*(cd|chdir|Set-Location)\s+(.+?)\s*$", re.IGNORECASE)


class ShellTool(Tool):
    name = "Shell"
    description = (
        "执行一条 Windows PowerShell 命令，返回标准输出与错误。cwd 跨调用延续。"
        "优先使用专用工具（读取 / 搜索 / 编辑 / 写入），仅在没有对应专用工具时使用本工具。"
    )
    permission = PermissionLevel.EXECUTE
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 PowerShell 命令。",
            }
        },
        "required": ["command"],
    }

    def __init__(self) -> None:
        self._cwd: Path | None = None
        self._proc: subprocess.Popen | None = None

    def execute(self, arguments: dict) -> str:
        command = arguments.get("command", "")
        if not command or not str(command).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：command")

        # 目录切换不派生子进程，直接更新会话内 cwd 状态，保证跨调用延续
        cd_match = _CD_RE.match(str(command))
        if cd_match:
            return self._change_dir(cd_match.group(2).strip())

        if self._cwd is None:
            self._cwd = Path.cwd()

        try:
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", str(command)],
                cwd=str(self._cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise EikoCodeError(ErrorKind.TOOL_TARGET_MISSING, type(exc).__name__) from exc

        self._proc = proc
        try:
            out, err = proc.communicate()
        finally:
            self._proc = None

        if proc.returncode != 0:
            tail = (err or "").strip()[:1000]
            raise EikoCodeError(
                ErrorKind.TOOL_COMMAND_FAILED,
                f"命令以非零退出码 {proc.returncode} 结束。\n{tail}",
            )

        out = (out or "").strip()
        err_tail = (err or "").strip()
        result = out or "(无输出)"
        if err_tail:
            result += f"\n[stderr]\n{err_tail}"
        return result

    def current_cwd(self) -> Path:
        """当前工作目录（跨调用延续的会话内 cwd）。

        供环境探测使用：主进程的 `Path.cwd()` 不随本工具的 `cd` 变化，
        真实工作目录只存在于此。
        """
        return self._cwd or Path.cwd()

    def _change_dir(self, target: str) -> str:
        base = self._cwd or Path.cwd()
        resolved = (base / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        if not resolved.exists():
            return f"目录不存在：{target}"
        self._cwd = resolved
        return f"已切换到 {resolved}"

    def kill(self) -> None:
        """超时由执行器调用：杀掉整个进程树（含子进程），不留孤儿。"""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        pid = proc.pid
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
