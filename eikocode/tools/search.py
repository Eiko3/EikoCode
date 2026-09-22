"""按模式搜索（glob）与按内容搜索（grep），均为只读权限。"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from .base import PermissionLevel, Tool

# 结果条数上限，避免一次搜索吐回海量内容撑爆上下文
MAX_MATCHES = 500
# 单文件大小上限，超过则跳过（避免为搜索读完大文件）
_MAX_FILE_BYTES = 4 * 1024 * 1024
_BINARY_PROBE_BYTES = 8192


class GlobTool(Tool):
    name = "Glob"
    description = "按文件名模式递归搜索文件，返回匹配的路径列表。"
    permission = PermissionLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "glob 模式，如 *.py 或 src/**/test_*.py。",
            },
            "path": {
                "type": "string",
                "description": "搜索根目录，默认当前工作目录。",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, arguments: dict) -> str:
        pattern = arguments.get("pattern", "")
        if not pattern or not str(pattern).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：pattern")
        root = Path(arguments.get("path") or ".")
        if not root.exists():
            raise EikoCodeError(
                ErrorKind.TOOL_TARGET_MISSING, f"搜索根目录不存在：{root}"
            )
        matches = [str(p) for p in root.rglob(str(pattern)) if p.is_file()]
        matches = matches[:MAX_MATCHES]
        if not matches:
            return "无匹配文件。"
        return "\n".join(matches)


class GrepTool(Tool):
    name = "Grep"
    description = "按内容（正则）递归搜索文件，返回 文件:行号:内容 格式。"
    permission = PermissionLevel.READ
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要匹配的正则表达式。",
            },
            "path": {
                "type": "string",
                "description": "搜索根目录，默认当前工作目录。",
            },
        },
        "required": ["pattern"],
    }

    def execute(self, arguments: dict) -> str:
        pattern = arguments.get("pattern", "")
        if not pattern or not str(pattern).strip():
            raise EikoCodeError(ErrorKind.TOOL_INVALID_ARG, "缺少参数：pattern")
        root = Path(arguments.get("path") or ".")
        if not root.exists():
            raise EikoCodeError(
                ErrorKind.TOOL_TARGET_MISSING, f"搜索根目录不存在：{root}"
            )
        try:
            regex = re.compile(str(pattern))
        except re.error as exc:
            raise EikoCodeError(
                ErrorKind.TOOL_INVALID_ARG, f"正则表达式不合法：{exc}"
            )

        results: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_FILE_BYTES:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:_BINARY_PROBE_BYTES]:
                continue
            try:
                text = data.decode("utf-8", errors="ignore")
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{path}:{lineno}:{line}")
                    if len(results) >= MAX_MATCHES:
                        return "\n".join(results)

        if not results:
            return "无匹配内容。"
        return "\n".join(results)
