"""文件读取工具。

只读权限。带两类保护：超过上限的大文件、含 NUL 的二进制文件，都直接拒绝，
不崩溃、不吐乱码——交给模型去改用更精确的搜索。文件内容原样返回（含原换行符）。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from .base import PermissionLevel, Tool

# 读取上限 1 MB（提议默认，见 checklist.md 第 12 组）
MAX_READ_BYTES = 1 * 1024 * 1024
# 二进制探测只看文件头这一截，避免为判断类型读完整个大文件
_BINARY_PROBE_BYTES = 8192


class ReadFileTool(Tool):
    name = "ReadFile"
    description = "读取一个文本文件的内容并返回。对大文件与二进制文件会拒绝。"
    permission = PermissionLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径（相对或绝对）。",
            }
        },
        "required": ["path"],
    }

    def execute(self, arguments: dict) -> str:
        raw = arguments.get("path", "")
        if not raw or not str(raw).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：path")
        path = Path(raw)
        if not path.exists():
            raise EikoCodeError(ErrorKind.TOOL_TARGET_MISSING, f"文件不存在：{path}")
        if path.is_dir():
            raise EikoCodeError(
                ErrorKind.TOOL_TARGET_MISSING, f"目标是目录而非文件：{path}"
            )

        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            raise EikoCodeError(
                ErrorKind.TOOL_TOO_LARGE,
                "文件过大，请改用更精确的搜索（如 Glob 或 Grep），或只读其中的一部分。",
            )

        data = path.read_bytes()
        probe = data[:_BINARY_PROBE_BYTES]
        if b"\x00" in probe:
            raise EikoCodeError(
                ErrorKind.TOOL_BINARY, "看起来是二进制文件，无法以文本方式读取。"
            )

        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise EikoCodeError(
                ErrorKind.TOOL_BINARY, "看起来不是 UTF-8 文本文件，无法以文本方式读取。"
            )
