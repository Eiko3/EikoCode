"""多段编辑工具。写权限。原子语义。

一次请求可传多段 `{old_string, new_string}`。全部匹配成功才整体落盘；任一段
不匹配或不唯一，则整次不写入，文件保持原样。写入前校验文件未被外部改动，
防止覆盖用户手上的最新内容。不用于创建新文件（新建走 WriteFile）。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from .base import PermissionLevel, Tool


class EditFileTool(Tool):
    name = "EditFile"
    description = (
        "对已有文件做多段精确替换。编辑前必须先读该文件（用读取工具查看当前内容）；"
        "所有替换段唯一匹配才落盘，任一段不匹配则整次不写入。"
    )
    permission = PermissionLevel.WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要编辑的文件路径。"},
            "edits": {
                "type": "array",
                "description": "替换段列表，每段含 old_string 与 new_string。",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "待替换的文本，必须在文件中唯一出现。",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "替换后的文本。",
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    }

    def execute(self, arguments: dict) -> str:
        raw_path = arguments.get("path", "")
        edits = arguments.get("edits") or []
        if not raw_path or not str(raw_path).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：path")
        if not isinstance(edits, list) or not edits:
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：edits（至少一段）")

        path = Path(raw_path)
        if not path.exists():
            raise EikoCodeError(
                ErrorKind.TOOL_TARGET_MISSING,
                f"文件不存在：{path}（EditFile 不用于创建新文件，新建请用 WriteFile）",
            )
        if path.is_dir():
            raise EikoCodeError(
                ErrorKind.TOOL_TARGET_MISSING, f"目标是目录而非文件：{path}"
            )

        # newline='' 保留原换行符，读出来的文本里 \r\n 原样保留
        original = path.read_text(encoding="utf-8", newline="", errors="replace")
        snapshot = path.stat()
        newline = "\r\n" if "\r\n" in original else "\n"

        # 先校验所有段（原子：任一失败都不落盘）
        text = original
        for index, edit in enumerate(edits):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if old == "":
                raise EikoCodeError(
                    ErrorKind.TOOL_INVALID_ARG,
                    f"第 {index + 1} 段 old_string 为空，无法唯一定位。",
                )
            count = text.count(old)
            if count == 0:
                raise EikoCodeError(
                    ErrorKind.TOOL_ERROR,
                    f"整次编辑未生效：第 {index + 1} 段未找到待替换文本「{old[:40]}」。",
                )
            if count > 1:
                raise EikoCodeError(
                    ErrorKind.TOOL_ERROR,
                    f"匹配不唯一，请提供更精确的上下文：待替换文本在文件中出现 {count} 次。",
                )
            text = text.replace(old, new.replace("\n", newline), 1)

        # 写入前再校验：文件是否在我们读取后被外部改动
        current = path.stat()
        if (
            current.st_mtime_ns != snapshot.st_mtime_ns
            or current.st_size != snapshot.st_size
        ):
            raise EikoCodeError(
                ErrorKind.TOOL_ERROR,
                "文件已被外部改动，已拒绝覆盖以防丢失你的改动。",
            )

        try:
            path.write_text(text, encoding="utf-8", newline="")
        except PermissionError as exc:
            raise EikoCodeError(ErrorKind.TOOL_PERMISSION_DENIED) from exc
        except OSError as exc:
            raise EikoCodeError(ErrorKind.TOOL_TARGET_MISSING) from exc

        return f"已应用 {len(edits)} 处编辑。"
