"""快照存储（v7）：超大工具结果的完整原文落盘。

快照目录位于项目内（提议默认 `.eikocode/snapshots/`），文件名含时间戳
与来源工具名；同秒多次写入加序号，不互相覆盖。只创建，不清理——
清理交给用户（spec.md §3 能力 58）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..errors import ErrorKind, EikoCodeError

SNAPSHOT_DIRNAME = ".eikocode/snapshots"

_SAFE_RE = re.compile(r"[^\w-]+")


def _safe_tool_name(tool_name: str) -> str:
    cleaned = _SAFE_RE.sub("_", tool_name or "tool").strip("_")
    return cleaned or "tool"


def save_snapshot(content: str, tool_name: str, base_dir: Path | None = None) -> Path:
    """把完整内容写入快照，返回路径。写盘失败抛既有错误类别。"""
    base = Path(base_dir) if base_dir else Path.cwd()
    directory = base / SNAPSHOT_DIRNAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = _safe_tool_name(tool_name)
        path = directory / f"{stamp}-{stem}.txt"
        counter = 1
        while path.exists():
            path = directory / f"{stamp}-{counter}-{stem}.txt"
            counter += 1
        path.write_text(content, encoding="utf-8")
        return path
    except OSError as exc:
        raise EikoCodeError(ErrorKind.TOOL_ERROR, f"快照写入失败：{exc}") from exc
