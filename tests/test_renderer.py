"""吉祥物与启动横幅的渲染测试。全部离线，用 StringIO 捕获输出。"""

from io import StringIO

import pytest
from rich.console import Console

from eikocode.renderer import MASCOT_ENV, MASCOT_LINES, Renderer


def _renderer(color: bool):
    buffer = StringIO()
    # 往 StringIO 输出时 Rich 拿不到真实终端句柄，会自动放弃着色。
    # 用 color_system 绕开终端探测，才能验证彩色分支确实走了着色路径。
    console = Console(
        file=buffer,
        no_color=not color,
        highlight=False,
        soft_wrap=True,
        width=100,
        color_system="truecolor" if color else None,
    )
    return Renderer(color, console), buffer


@pytest.fixture(autouse=True)
def _mascot_on(monkeypatch):
    monkeypatch.delenv(MASCOT_ENV, raising=False)


def test_banner_draws_every_mascot_line():
    renderer, buffer = _renderer(False)
    renderer.banner(["EikoCode v1"])
    out = buffer.getvalue()
    for row in MASCOT_LINES:
        assert row.strip() in out


def test_banner_shows_side_text():
    renderer, buffer = _renderer(False)
    renderer.banner(["EikoCode v1", "模型：deepseek-chat"])
    out = buffer.getvalue()
    assert "EikoCode v1" in out
    assert "模型：deepseek-chat" in out


def test_banner_ignores_blank_side_lines():
    renderer, buffer = _renderer(False)
    renderer.banner(["EikoCode v1", "   ", ""])
    out = buffer.getvalue()
    for row in MASCOT_LINES:
        assert row.strip() in out


def test_banner_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(MASCOT_ENV, "off")
    renderer, buffer = _renderer(False)
    renderer.banner(["EikoCode v1"])
    assert buffer.getvalue().strip() == ""


@pytest.mark.parametrize("value", ["0", "off", "OFF", "false", "no", "none"])
def test_mascot_off_values(monkeypatch, value):
    monkeypatch.setenv(MASCOT_ENV, value)
    renderer, _ = _renderer(False)
    assert renderer.mascot_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "yes", ""])
def test_mascot_on_values(monkeypatch, value):
    monkeypatch.setenv(MASCOT_ENV, value)
    renderer, _ = _renderer(False)
    assert renderer.mascot_enabled() is True


def test_degraded_banner_emits_no_escape_sequences():
    renderer, buffer = _renderer(False)
    renderer.banner(["EikoCode v1"])
    assert "\x1b[" not in buffer.getvalue()


def test_colored_banner_emits_escape_sequences():
    renderer, buffer = _renderer(True)
    renderer.banner(["EikoCode v1"])
    assert "\x1b[" in buffer.getvalue()
