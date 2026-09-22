"""Git 工作树隔离（v13）：目录名安全校验。

校验在任何 git 子进程调用之前完成——LLM 输入的恶意路径根本没有机会
触达 git（spec.md §3 能力 102）。
"""

from __future__ import annotations

import re

# 目录名：小写字母数字、`/` 嵌套分隔、`-`、`_`；总长 ≤64
_NAME_RE = re.compile(r"^[a-z0-9/_-]{1,64}$")

# 分支命名空间前缀
BRANCH_PREFIX = "worktrees/"


def validate_name(name: str) -> str | None:
    """校验工作树目录名。返回 None 表示合法，否则返回错误原因。"""
    name = str(name or "").strip()
    if not name:
        return "目录名不能为空"
    if len(name) > 64:
        return "目录名过长（最多 64 字符）"
    if not _NAME_RE.match(name):
        return f"目录名含非法字符（仅允许小写字母、数字、/、-、_）：{name}"
    if name.startswith("/") or name.endswith("/"):
        return "目录名不能以 / 开头或结尾"
    segments = name.split("/")
    if any(seg in (".", "..") for seg in segments):
        return "目录名不允许 . 或 .. 路径段"
    if any(seg == "" for seg in segments):
        return "目录名不允许空的路径段（连续 //）"
    return None


def branch_for(name: str) -> str:
    """目录名 → 分支名：嵌套分隔符平铺为 `-`（如 feature/x → worktrees/feature-x）。"""
    return BRANCH_PREFIX + name.replace("/", "-")


def is_valid_name(name: str) -> bool:
    return validate_name(name) is None
