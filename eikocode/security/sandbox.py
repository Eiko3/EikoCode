"""路径沙箱（v5）：流水第二层。

文件类工具的路径参数规范化后判定是否落在允许目录内；越界直接拒绝
（不进确认流程）。允许目录 = 工作目录（默认）+ 配置追加 + 系统临时目录
（可写，工具性写入是刚需）。读与写同一套边界（spec.md §3 能力 37）。

明示边界：只覆盖带 `path` 参数的工具；命令执行类工具不做路径检查。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Sequence

from .decision import ACTION_DENY, Decision


def _normcase(path: Path) -> str:
    """Windows 下归一大小写与分隔符，让 `c:\\` 与 `C:\\`、`/` 与 `\\` 等价。"""
    return os.path.normcase(str(path))


def _norm_dir(path: Path) -> str:
    return _normcase(path.resolve())


class PathSandbox:
    """允许目录集合与越界判定。"""

    def __init__(
        self,
        extra_dirs: Sequence[str | Path] = (),
        base_dir: Path | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        # 基准目录在构造时固定：运行中（哪怕 Shell 换了目录）相对路径的
        # 判定基准不漂移——可预测优先。
        self._base = base_dir or Path.cwd()
        self._allowed_raw = [str(self._base)] + [str(d) for d in extra_dirs]
        self._allowed = [_norm_dir(Path(d)) for d in self._allowed_raw]
        temp = temp_dir or Path(tempfile.gettempdir())
        self._temp = _norm_dir(temp)

    def check(self, arguments: dict) -> Decision | None:
        """检查路径参数。返回 None 表示本层无裁决（不在允许目录判定范围，
        或路径在允许范围内），交由下一层；越界返回拒绝裁决。"""
        raw = arguments.get("path")
        if raw is None:
            return None

        raw_text = str(raw)
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = self._base / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return self._deny(raw_text)

        normalized = _normcase(resolved)
        for allowed in self._allowed:
            if normalized == allowed or normalized.startswith(allowed + os.sep):
                return None
        if normalized == self._temp or normalized.startswith(self._temp + os.sep):
            return None
        return self._deny(raw_text)

    def _deny(self, raw_text: str) -> Decision:
        allowed_desc = "；".join(self._allowed_raw)
        return Decision(
            ACTION_DENY,
            f"路径越界：{raw_text} 不在允许目录内（允许：{allowed_desc}；系统临时目录可写）",
        )
