"""本地 OpenAI 兼容流式 mock 服务，供端到端测试离线跑通主流程。

只干一件事：模拟 /chat/completions 的 SSE 流式响应，并记下每一笔请求，
让测试用例断言「阈值拦截时确实没有发起网络请求」。

mock 不联网、不依赖任何供应商 SDK，纯标准库。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

_DIGITS_RE = re.compile(r"(\d{3,})")


def _compose_reply(messages: List[dict]) -> str:
    """根据历史消息构造一个能体现「多轮记忆」的回复。

    - 若最后一条用户消息在问「第一轮记的什么」，就从更早的消息里翻出编号回显；
    - 否则回一条确认，并复述最近一条用户消息的尾巴，方便断言中文往返。
    """
    last = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last = m.get("content", "")
            break

    remembered = None
    for m in messages:
        if m.get("role") == "user" and "紫水晶" in m.get("content", ""):
            hit = _DIGITS_RE.search(m.get("content", ""))
            if hit:
                remembered = hit.group(1)
            break

    if "第一轮" in last or "记的什么" in last or "记的是什么" in last:
        if remembered:
            return f"你第一轮记的编号是：{remembered}。"
        return "我没记住任何编号。"

    tail = last[-12:] if last else ""
    return f"已收到你的消息：{tail}"


class MockOpenAIServer(ThreadingHTTPServer):
    """线程安全的 mock；request_count / requests 供测试断言。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), _Handler)
        self.request_count = 0
        self.requests: List[str] = []
        self._lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server_version = "eikocode-mock/1.0"

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_GET(self):  # 健康探针
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            data = {}

        messages = data.get("messages", [])
        model = data.get("model", "unknown")

        with self.server._lock:
            self.server.request_count += 1
            self.server.requests.append(model)

        reply = _compose_reply(messages)
        pieces = [reply[i : i + 6] for i in range(0, len(reply), 6)] or [reply]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        for piece in pieces:
            chunk = {
                "id": "mock",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                ],
            }
            payload = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
            self.wfile.write(b"data: " + payload + b"\r\n\r\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\r\n\r\n")
        self.wfile.flush()


def start_mock_server(host: str = "127.0.0.1") -> MockOpenAIServer:
    """起一个守护线程的服务，返回实例（含 .server_address[1] 实际端口）。"""
    server = MockOpenAIServer(host)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
