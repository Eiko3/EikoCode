"""stdio 传输（v6）：本地子进程，经标准输入输出收发成帧消息。

后台线程持续读行：应答进队列供 request 取用，通知（无标识报文）单独
留存，避免通知把应答的匹配搞乱。关闭时先温和关闭再强杀进程树
（沿用 v2「不留孤儿进程」的语义）。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from ..errors import ErrorKind, EikoCodeError
from . import protocol
from .transport import Transport

# 温和关闭的等待上限（秒），超时即强杀（提议默认 5，见 checklist 组 43）
GRACE_SECONDS = 5.0


class StdioTransport(Transport):
    def __init__(self, command: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
        self._command = list(command)
        self._env = dict(env or {})
        self._cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._responses: queue.Queue[dict] = queue.Queue()
        self._notifications: list[dict] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        if self._proc is not None:
            return
        env_full = dict(os.environ)
        env_full.update(self._env)
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env_full,
                cwd=self._cwd,
            )
        except (OSError, ValueError) as exc:
            raise EikoCodeError(ErrorKind.CONFIG, f"无法启动子进程：{exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = protocol.loads(line)
                except EikoCodeError:
                    continue
                if protocol.is_response(obj):
                    self._responses.put(obj)
                else:
                    with self._lock:
                        self._notifications.append(obj)
        except (ValueError, OSError):
            return

    def request(self, payload: dict, timeout: float) -> dict:
        self._write(payload)
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise EikoCodeError(ErrorKind.TIMEOUT, "等待外部工具服务响应超时") from exc

    def notify(self, payload: dict) -> None:
        self._write(payload)

    def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise EikoCodeError(ErrorKind.PROTOCOL, "外部工具服务未连接")
        try:
            proc.stdin.write(protocol.dumps(payload) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise EikoCodeError(ErrorKind.PROTOCOL, f"向外部工具服务写入失败：{exc}") from exc

    def drain_notifications(self) -> list[dict]:
        with self._lock:
            items, self._notifications = self._notifications, []
        return items

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is None:
            self.kill()
        self._proc = None

    def kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=GRACE_SECONDS,
            )
        except Exception:  # 杀不掉也要继续，不让异常冒到上层
            try:
                proc.kill()
            except Exception:
                pass
