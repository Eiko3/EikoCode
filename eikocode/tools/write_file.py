"""文件写入工具。写权限。

覆盖创建/覆盖文件。原文件的换行符（CRLF / LF）在写入后保持不变：
若文件已存在，沿用其换行风格；新建文件默认用 CRLF（Windows 约定）。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from .base import PermissionLevel, Tool


class WriteFileTool(Tool):
    name = "WriteFile"
    description = (
        "创建新文件，或覆盖已有文件（覆盖前必须先读该文件确认当前内容）。"
        "新建文件默认使用 CRLF 换行（Windows 约定）。"
    )
    permission = PermissionLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径。"},
            "content": {"type": "string", "description": "文件的完整内容。"},
        },
        "required": ["path", "content"],
    }

    def execute(self, arguments: dict) -> str:
        raw_path = arguments.get("path", "")
        content = arguments.get("content", "")
        if not raw_path or not str(raw_path).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：path")
        if content is None:
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：content")
        path = Path(raw_path)

        if path.exists():
            # 沿用已存在文件的换行风格，避免悄悄改写整文件换行符
            existing = path.read_bytes()
            newline = "\r\n" if b"\r\n" in existing else "\n"
        else:
            newline = "\r\n"

        if newline == "\r\n":
            data = str(content).replace("\n", "\r\n")
        else:
            data = str(content)

        try:
            path.write_text(data, encoding="utf-8", newline="")
        except PermissionError as exc:
            raise EikoCodeError(ErrorKind.TOOL_PERMISSION_DENIED) from exc
        except OSError as exc:
            raise EikoCodeError(ErrorKind.TOOL_TARGET_MISSING) from exc

        return f"已写入文件：{path}（{len(data)} 字节）"
