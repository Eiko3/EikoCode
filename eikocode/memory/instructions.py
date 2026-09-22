"""项目指令文件（v8）：EIKOCODE.md 的发现与 @include 展开。

两级：项目根 EIKOCODE.md（优先级高，排前）+ 用户级 ~/.eikocode/EIKOCODE.md。
注入对话最早位置、会话内固定（进入缓存前缀）；两级都不存在属于正常态，
静默返回空（spec.md §3 能力 59、61）。

@include 规则（提议默认，见 checklist 组 59）：行首 `@相对路径` 展开为
被引用文件完整内容；嵌套深度 ≤5；循环引用只展开一次；引用路径相对所在
文件解析，resolve 后不得超出项目根（项目级）或用户级目录（用户级）；
缺失或非法的引用行直接消失。
"""

from __future__ import annotations

from pathlib import Path

INSTRUCTION_FILENAME = "EIKOCODE.md"

# @include 嵌套深度上限（提议默认 5，见 checklist 组 59）
MAX_INCLUDE_DEPTH = 5


def _expand_includes(
    text: str,
    base_dir: Path,
    root_dir: Path,
    depth: int = 0,
    visited: set[Path] | None = None,
) -> str:
    """逐行展开 @引用。越界 / 循环 / 超深 / 缺失的引用行直接消失。"""
    visited = visited if visited is not None else set()
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("@"):
            out_lines.append(line)
            continue
        ref = stripped[1:].strip()
        if depth >= MAX_INCLUDE_DEPTH or not ref:
            continue  # 超深 / 空引用：跳过
        try:
            ref_path = (base_dir / ref).resolve()
        except OSError:
            continue
        root = root_dir.resolve()
        if not ref_path.is_relative_to(root):
            continue  # 越出允许范围：跳过
        if ref_path in visited or not ref_path.is_file():
            continue  # 循环 / 缺失：跳过
        visited.add(ref_path)
        try:
            included = ref_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out_lines.append(
            _expand_includes(
                included, ref_path.parent, root_dir, depth + 1, visited
            )
        )
    return "\n".join(out_lines)


def load_instructions(
    project_dir: Path | None = None, user_dir: Path | None = None
) -> tuple[str, list[str]]:
    """读取两级指令文件。返回（拼接内容, 已加载文件描述列表）。

    项目级在前；内容经 @include 展开。都没有时返回空串——调用方据此
    跳过注入。
    """
    parts: list[str] = []
    loaded: list[str] = []

    sources: list[tuple[Path, Path, str]] = []
    if project_dir:
        sources.append(
            (project_dir / INSTRUCTION_FILENAME, project_dir, str(INSTRUCTION_FILENAME))
        )
    if user_dir:
        sources.append(
            (user_dir / INSTRUCTION_FILENAME, user_dir, f"用户级 {INSTRUCTION_FILENAME}")
        )

    for path, root, label in sources:
        try:
            resolved = path.resolve()
            if not resolved.is_file():
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        expanded = _expand_includes(text, path.parent, path.parent, 0, {resolved})
        if expanded.strip():
            parts.append(expanded.strip())
            loaded.append(f"{label}（{len(expanded.splitlines())} 行）")

    return "\n\n".join(parts), loaded
