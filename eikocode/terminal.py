"""Windows 终端能力探测与编码钉死。

两个职责：
1. 把 stdout/stderr 的编码钉死为 utf-8，让中文不受系统代码页（GBK 936）影响。
2. 探测当前终端能不能吃 ANSI 转义序列，探测失败就静默降级，不打断启动。
"""

from __future__ import annotations

import os
import sys

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

DEGRADED_NOTICE = "终端不支持彩色输出，已切换为纯文本模式。"


def ensure_utf8() -> None:
    """把标准流的编码强制为 utf-8，覆盖系统代码页。

    stdin 同样要钉：管道 / 重定向输入下它默认按 ANSI 代码页（GBK）解码，
    UTF-8 的中文字节会被解出代理字符（surrogate），原样进入会话历史后，
    发请求时 SDK 的 JSON 编码会直接抛 UnicodeEncodeError 崩溃。
    errors="replace" 保证再怪的输入也只是丢字符，不会让程序崩。

    在被测环境里流可能被替换成非 TextIOWrapper 的对象，
    这里失败即跳过，不影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        try:
            stdin_reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def detect_color_support() -> bool:
    env = os.environ
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False

    stream = sys.stdout
    isatty = getattr(stream, "isatty", None)
    if isatty is None or not isatty():
        return False

    # Windows Terminal 一定支持，直接在 shell 层判定，省掉一次系统调用。
    if env.get("WT_SESSION"):
        return True
    if sys.platform != "win32":
        return True
    return _enable_windows_virtual_terminal()


def _enable_windows_virtual_terminal() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False

        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not kernel32.SetConsoleMode(
            handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        ):
            return False
        return True
    except (OSError, AttributeError, ValueError, TypeError):
        return False
