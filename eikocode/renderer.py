"""呈现层：流式渲染与降级。

彩色模式下用 Live 反复重绘 Markdown，让代码块在流式中途就带上边框和高亮；
降级模式下老老实实逐块 print，一个转义序列都不输出。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Sequence

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

# 两次重绘之间的最小间隔。流式吐得很快时用它挡住无意义的重排。
_REFRESH_INTERVAL = 0.05

# 白色小猫吉祥物。必须是 raw string：猫耳朵里的反斜杠会被 Python 当成转义符。
MASCOT_LINES = (
    r"   /\_/\   ",
    r"  ( ^.^ )  ",
    r"   > ^ <   ",
    r"  /|   |\  ",
    r" (_|   |_) ",
)

# 浅色背景终端上看不见白猫，用这个变量关掉它。
MASCOT_ENV = "EIKOCODE_MASCOT"
MASCOT_OFF_VALUES = frozenset({"0", "off", "false", "no", "none"})

MASCOT_STYLE = "bright_white"
MASCOT_TEXT_STYLE = "bright_white bold"


class Renderer:
    def __init__(self, color: bool, console: Console | None = None) -> None:
        self._color = color
        self._console = console or Console(
            no_color=not color, highlight=color, soft_wrap=True
        )
        self._buffer: list[str] = []
        self._live: Live | None = None
        self._last_refresh = 0.0

    @property
    def color(self) -> bool:
        return self._color

    def info(self, text: str = "") -> None:
        self._console.print(text)

    def notice(self, text: str) -> None:
        self._console.print(text, style="yellow")

    def error(self, text: str) -> None:
        self._console.print(text, style="red")

    def mascot_enabled(self) -> bool:
        """吉祥物只在深色终端上有意义，浅色背景下白猫会消失。"""
        value = os.environ.get(MASCOT_ENV, "").strip().lower()
        return value not in MASCOT_OFF_VALUES

    def banner(self, lines: Sequence[str]) -> None:
        """启动时画一只白色小猫，右侧竖排跟着几行品牌信息。"""
        if not self.mascot_enabled():
            return
        art = list(MASCOT_LINES)
        side = [str(item) for item in lines if str(item).strip()]
        # 文字比猫矮时让它对着猫的中间，不要顶在最上面
        top = max(0, (len(art) - len(side)) // 2)
        width = max(len(row) for row in art)
        for index, row in enumerate(art):
            text = side[index - top] if 0 <= index - top < len(side) else ""
            if self._color:
                line = Text()
                line.append(row.ljust(width), style=MASCOT_STYLE)
                line.append(f"  {text}".rstrip(), style=MASCOT_TEXT_STYLE)
                self._console.print(line)
            else:
                self._console.print(f"{row.ljust(width)}  {text}".rstrip())
        self._console.print()

    def print_help(self, commands: Sequence[str]) -> None:
        self._console.print("可用命令：")
        for name in commands:
            self._console.print(f"  {name}")

    def usage(self, used: int, limit: int, turns: int, cache: tuple[int, int] | None = None) -> None:
        """用量视图。cache = (命中 tokens, 输入 tokens)，无数据时不显示。

        缓存未生效时照常显示为零——「没生效」要可见，不是隐藏。
        """
        pct = round(used / limit * 100) if limit > 0 else 100
        self._console.print(f"已用 {used:,} / {limit:,}（{pct}%）· {turns} 轮")
        if cache is not None:
            hit, total = cache
            rate = round(hit / total * 100) if total > 0 else 0
            self._console.print(f"缓存命中 {hit:,} / {total:,}（{rate}%）")

    def begin_stream(self) -> None:
        self._buffer = []
        self._last_refresh = 0.0
        if not self._color:
            return
        self._live = Live(
            console=self._console,
            refresh_per_second=12,
            vertical_overflow="visible",
        )
        self._live.start()

    def feed(self, chunk: str) -> None:
        self._buffer.append(chunk)
        if self._live is None:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return
        now = time.perf_counter()
        if now - self._last_refresh >= _REFRESH_INTERVAL:
            self._last_refresh = now
            self._live.update(Markdown("".join(self._buffer)))

    def end_stream(self) -> None:
        if self._live is None:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._buffer = []
            return
        self._live.update(Markdown("".join(self._buffer)))
        self._live.stop()
        self._live = None
        self._buffer = []
