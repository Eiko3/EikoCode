"""Tab 补全（v9）：候选来自注册表，逻辑与输入环境解耦。

补全规则（提议默认，见 checklist 组 71）：唯一匹配直接补全；多重匹配
返回公共前缀并列出候选；无匹配保持原样；隐藏命令不参与。

输入环境：Windows 走 pyreadline3（可选依赖，导入失败静默降级，功能
不受影响）；补全的核心判定在本模块的纯函数里，可离线单测。
"""

from __future__ import annotations

from typing import Callable, Sequence


def complete_command(text: str, candidates: Sequence[str]) -> tuple[str, list[str]]:
    """对当前输入做命令补全。返回（补全后的文本, 多匹配候选列表）。

    无匹配 / 多匹配无公共前缀时原样返回；候选列表仅在多重匹配时非空。
    """
    if not text.startswith("/"):
        return text, []
    matches = sorted(c for c in candidates if c.startswith(text))
    if not matches:
        return text, []
    if len(matches) == 1:
        return matches[0] + " ", []
    common = matches[0]
    for m in matches[1:]:
        while not m.startswith(common):
            common = common[:-1]
    return common, matches


def install_readline_completion(candidates_fn: Callable[[], list[str]]) -> bool:
    """尝试挂接 readline 补全。成功返回 True；环境不支持时静默降级。

    Windows 下依赖 pyreadline3 提供的 readline 模块（可选依赖，
    见 checklist 组 71）；未安装时功能完好，只是没有 Tab 补全。
    """
    try:
        import readline
    except ImportError:
        return False

    class _Completer:
        def __init__(self) -> None:
            self._cache: list[str] = []

        def complete(self, text: str, state: int):
            if state == 0:
                self._cache = [c for c in candidates_fn() if c.startswith(text)]
            if state < len(self._cache):
                return self._cache[state]
            return None

    readline.set_completer(_Completer().complete)
    readline.parse_and_bind("tab: complete")
    return True
