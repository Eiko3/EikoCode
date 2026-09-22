"""Streamable HTTP 传输（v6）：远程服务端点。

一次请求一个 POST；响应可能是普通 JSON，也可能是 SSE 流（多条事件）。
服务端下发的会话标识在后续请求中回带。连接超时与读取超时分别设置；
网络与超时异常归一为既有错误类别。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..errors import ErrorKind, EikoCodeError
from . import protocol
from .transport import Transport

# 连接超时（提议默认 10 秒，见 checklist 组 44）
CONNECT_TIMEOUT = 10.0
# 读取超时沿用既有执行器超时（120 秒）
READ_TIMEOUT = 120.0

ACCEPT = "application/json, text/event-stream"


class HttpTransport(Transport):
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self._url = url
        self._headers = dict(headers or {})
        self._session_id: str | None = None
        self._client: httpx.Client | None = None

    def start(self) -> None:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT)
            )

    def request(self, payload: dict, timeout: float) -> dict:
        response = self._post(payload, timeout, expect_body=True)
        return self._first_response(response)

    def notify(self, payload: dict) -> None:
        # 通知不期待响应体；服务端通常回 202。
        try:
            self._post(payload, CONNECT_TIMEOUT, expect_body=False)
        except EikoCodeError:
            pass  # 通知失败不该打断流程

    def _post(self, payload: dict, timeout: float, expect_body: bool) -> httpx.Response:
        if self._client is None:
            self.start()
        assert self._client is not None
        headers = {
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            return self._client.post(
                self._url,
                content=protocol.dumps(payload).encode("utf-8"),
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise EikoCodeError(ErrorKind.TIMEOUT, "外部工具服务响应超时") from exc
        except httpx.HTTPError as exc:
            raise EikoCodeError(ErrorKind.NETWORK, f"外部工具服务连接失败：{type(exc).__name__}") from exc

    def _first_response(self, response: httpx.Response) -> dict:
        # 会话标识：服务端下发即保存，后续请求回带
        sid = response.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        ctype = response.headers.get("Content-Type", "")
        text = response.text
        if "text/event-stream" in ctype:
            return self._parse_sse(text)
        try:
            obj = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise EikoCodeError(ErrorKind.PROTOCOL, f"响应解析失败：{exc}") from exc
        if not isinstance(obj, dict):
            raise EikoCodeError(ErrorKind.PROTOCOL, "响应不是对象")
        if "error" not in obj and "result" not in obj:
            raise EikoCodeError(ErrorKind.PROTOCOL, "响应缺少结果")
        return obj

    @staticmethod
    def _parse_sse(text: str) -> dict:
        """从 SSE 文本里取出第一条应答（通知让位给带标识的应答）。"""
        fallback: dict | None = None
        for block in text.split("\n\n"):
            for line in block.splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    if protocol.is_response(obj):
                        return obj
                    fallback = fallback or obj
        if fallback is not None:
            return fallback
        raise EikoCodeError(ErrorKind.PROTOCOL, "SSE 流中没有可用的应答")

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def kill(self) -> None:
        """HTTP 没有常驻进程：关闭连接即视为终止在途请求。"""
        self.close()
