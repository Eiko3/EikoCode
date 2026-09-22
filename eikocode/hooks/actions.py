"""Hook 动作执行器（v11）：shell / prompt / http / subagent + 模板变量。

统一返回 ActionResult（成功 / 失败 + 输出文本）：
- tool_before 事件上「失败 = 拦截」，输出首行即拒绝原因；
- 其他事件上失败只记日志（错误隔离），不中断主流程。
模板变量 {event} {tool} {args.<key>} {message} {error} {cwd}，未定义替换空串。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..errors import EikoCodeError

# tool_after 载荷里结果文本的截断长度（提议默认，避免模板与请求体过大）
TOOL_RESULT_PREVIEW = 500

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


@dataclass
class ActionResult:
    success: bool
    output: str

    @property
    def reason(self) -> str:
        """拒绝原因：输出首行（截断到 200 字符）。"""
        line = self.output.strip().splitlines()[0] if self.output.strip() else ""
        return line[:200] or "（动作无输出）"


def render_template(text: str, context: dict) -> str:
    """模板变量替换：{event} {tool} {args.<key>} {message} {error} {cwd}。

    未定义变量替换为空串，不报错（spec.md §3 能力 89）。
    """

    def _lookup(match: re.Match) -> str:
        expr = match.group(1)
        if expr.startswith("args."):
            args = context.get("args")
            key = expr[5:]
            if isinstance(args, dict) and key in args:
                return str(args[key])
            return ""
        value = context.get(expr)
        return "" if value is None else str(value)

    return _VAR_RE.sub(_lookup, str(text))


def build_context(event: str, tool: str = "", args: dict | None = None,
                  message: str = "", error: str = "", cwd: str = "") -> dict:
    """构造事件上下文；tool_after 的 message 截断到预览长度。"""
    return {
        "event": event,
        "tool": tool,
        "args": args or {},
        "message": message[:TOOL_RESULT_PREVIEW],
        "error": error,
        "cwd": cwd,
    }


def execute_action(action: dict, context: dict, timeout: int, notice, inject=None) -> ActionResult:
    """按动作类型分发执行。执行器内部异常按失败处理（错误隔离兜底）。

    `inject`：提示词投递函数（运行时注入队列），prompt 动作必需。
    """
    action_type = str(action.get("type", ""))
    try:
        if action_type == "shell":
            return _run_shell(action, context, timeout, notice)
        if action_type == "prompt":
            return _run_prompt(action, context, notice, inject)
        if action_type == "http":
            return _run_http(action, context, timeout)
        if action_type == "subagent":
            return ActionResult(False, "子 Agent 动作未实现（v11 占位）")
        return ActionResult(False, f"未知动作类型：{action_type}")
    except Exception as exc:  # 错误隔离：执行器自身异常不外泄
        return ActionResult(False, f"{type(exc).__name__}: {exc}"[:200])


# --------------------------------------------------------------------------- #
def _run_shell(action: dict, context: dict, timeout: int, notice) -> ActionResult:
    command = render_template(str(action.get("command", "")), context)
    if not command.strip():
        return ActionResult(False, "shell 动作命令为空")
    notice(f"Hook 执行 shell：{command[:120]}")
    # 输出编码统一：先钉 PS 的控制台输出编码为 UTF-8（同 v1 ensure_utf8 原则），
    # 否则 PowerShell 5.1 按 GBK 输出中文，UTF-8 解码会得到乱码。
    full = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", full],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(False, f"shell 动作超时（{timeout} 秒）")
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    return ActionResult(ok, output.strip() or f"退出码 {proc.returncode}")


def _run_prompt(action: dict, context: dict, notice, inject) -> ActionResult:
    """注入提示词：交由调用方（engine）投递到运行时的注入队列。"""
    text = render_template(str(action.get("text", "")), context)
    if not text.strip():
        return ActionResult(False, "prompt 动作文本为空")
    if inject is None:
        return ActionResult(False, "注入通道不可用")
    inject(text)
    notice(f"Hook 注入提示词（下一轮请求生效）：{text[:120]}")
    return ActionResult(True, text)


def _run_http(action: dict, context: dict, timeout: int) -> ActionResult:
    url = render_template(str(action.get("url", "")), context)
    method = str(action.get("method", "POST")).upper()
    payload = json.dumps(
        {
            "event": context.get("event"),
            "tool": context.get("tool"),
            "args": context.get("args"),
            "message": context.get("message"),
            "error": context.get("error"),
            "cwd": context.get("cwd"),
        },
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return ActionResult(False, body or f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ActionResult(False, f"HTTP 请求失败：{exc}"[:200])
    ok = 200 <= status < 300
    return ActionResult(ok, body)


def fire_async(action: dict, context: dict, timeout: int, notice, inject=None) -> None:
    """后台线程执行动作（async: true）；完成只记日志，不阻塞主流程。"""

    def _worker() -> None:
        result = execute_action(action, context, timeout, notice, inject)
        if not result.success:
            notice(f"Hook 异步动作失败（已忽略）：{result.reason}")

    threading.Thread(target=_worker, daemon=True).start()
