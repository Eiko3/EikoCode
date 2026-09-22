"""JSON-RPC 2.0 消息层（v6 外部工具接入）。

只负责消息本身的构造、解析与错误归一：请求带标识、通知不带标识、
响应按标识关联。标识分配线程安全，允许多个请求并发在途（spec.md
§3 能力 43、44）。

checklist.md 组 42：错误码映射为提议默认。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from ..errors import ErrorKind, EikoCodeError

# 协议版本：随外部协议推进而调整，握手中与服务端协商。
PROTOCOL_VERSION = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_lock = threading.Lock()
_counter = 0


def next_id() -> int:
    """分配一个请求标识（整数自增、线程安全）。"""
    global _counter
    with _lock:
        _counter += 1
        return _counter


def make_request(method: str, params: dict | None = None) -> dict:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": next_id(), "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def make_notification(method: str, params: dict | None = None) -> dict:
    """通知：不带标识，对端不应答。"""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def make_error_response(rid: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "error": {"code": code, "message": message},
    }


def is_response(obj: dict) -> bool:
    """是否是对某个请求的应答（带标识且有 result / error）。"""
    return "id" in obj and ("result" in obj or "error" in obj)


def is_server_request(obj: dict) -> bool:
    """服务端主动发起的请求（有标识且带方法）——本期不支持，回方法不存在。"""
    return "id" in obj and "method" in obj and "result" not in obj and "error" not in obj


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def loads(line: str) -> dict:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise EikoCodeError(ErrorKind.PROTOCOL, f"消息解析失败：{exc}") from exc
    if not isinstance(obj, dict):
        raise EikoCodeError(ErrorKind.PROTOCOL, "消息不是对象")
    return obj


def raise_for_error(response: dict) -> None:
    """响应携带错误时抛出归一后的异常；正常响应直接返回。"""
    error = response.get("error")
    if not error:
        return
    code = error.get("code") if isinstance(error, dict) else None
    message = (
        error.get("message", "") if isinstance(error, dict) else str(error)
    )
    raise EikoCodeError(_kind_for_code(code), _describe(code, message))


def _kind_for_code(code: Any) -> ErrorKind:
    if code in (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS):
        return ErrorKind.PROTOCOL
    if code == INTERNAL_ERROR:
        return ErrorKind.TOOL_ERROR
    return ErrorKind.PROTOCOL


def _describe(code: Any, message: str) -> str:
    labels = {
        PARSE_ERROR: "解析失败",
        INVALID_REQUEST: "无效请求",
        METHOD_NOT_FOUND: "方法不存在",
        INVALID_PARAMS: "参数无效",
        INTERNAL_ERROR: "远端内部错误",
    }
    label = labels.get(code, "协议错误")
    return f"{label}（{code}）：{message}" if message else f"{label}（{code}）"
