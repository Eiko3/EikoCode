"""真实 CLI 进程级端到端验证（离线，靠本地 mock 服务）。

对应 checklist.md 第 8 节：
- E1 多轮记忆
- E3 供应商切换（/model 切换后请求确实用新模型）
- E5 中文往返（无乱码替换符）
- E6 阈值拦截（95% 时不出网络请求）
- 另附：E4 凭据缺失退出码、WARN 阈值仍发送

全部不联网：用 tests/mock_openai.py 起的本地流式服务冒充 OpenAI 兼容端点。
给子进程设 PYTHONUTF8=1，使管道里的中文输入按 UTF-8 解码（坐实 E5）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.mock_openai import start_mock_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(lines, model, openai_key="dummy-key"):
    server = start_mock_server()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/v1"

    env = dict(os.environ)
    env.pop("EIKOCODE_ANTHROPIC_API_KEY", None)
    env["EIKOCODE_OPENAI_API_KEY"] = openai_key
    env["EIKOCODE_OPENAI_BASE_URL"] = base_url
    env["EIKOCODE_MODEL"] = model
    env["PYTHONUTF8"] = "1"          # 强制 stdin/stdout UTF-8，验证中文往返
    env["TERM"] = "dumb"             # 走纯文本降级，避免 ANSI 干扰断言
    env["NO_COLOR"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "eikocode"],
        input="\n".join(lines),
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    server.shutdown()
    return proc, server


def test_e1_e3_e5_multi_turn_memory_and_switch_and_chinese():
    lines = [
        "记住这个编号：紫水晶 7391",
        "1 加 1 等于几",
        "我第一轮让你记的是什么",
        "/model deepseek-chat",
        "切换到新模型后再问一次：紫水晶编号是多少",
        "/exit",
    ]
    proc, server = _run_cli(lines, model="gpt-4o-mini")
    out = proc.stdout

    assert proc.returncode == 0, f"退出码应为 0，实际 {proc.returncode}，stderr={proc.stderr}"
    # E1 多轮记忆：第 3 轮能复述第 1 轮的编号
    assert "7391" in out, "E1 多轮记忆未命中 7391"
    # E3 供应商切换：命令提示 + 切换后请求确实用新模型
    assert "已切换模型：deepseek-chat" in out, "E3 切换提示缺失"
    assert "deepseek-chat" in server.requests, (
        f"E3 切换后请求未用新模型，实际请求序列={server.requests}"
    )
    # E5 中文往返：无乱码替换符，且中文原样保留
    assert "\ufffd" not in out, "E5 出现乱码替换符"
    assert "紫水晶" in out, "E5 中文未原样往返"
    assert "Traceback" not in out, "全链路不应泄漏堆栈"


def test_e6_threshold_block_makes_no_network_request():
    # 第一条超大消息历史为空→放行（请求 1 次）；第二条检查到历史已超 95%→拦截。
    big = "测" * 195000
    lines = [big, "继续", "/usage", "/exit"]
    proc, server = _run_cli(lines, model="gpt-4o-mini")
    out = proc.stdout

    assert "本次请求未发送" in out, "E6 未出现 95% 拦截提示"
    assert "已达 95%" in out, "E6 拦截文案缺失"
    # 关键：被拦截的那一轮没有发起任何网络请求。
    # v8 退出时的自动笔记（用户级 + 项目级各一次，会话有成功回复才触发）
    # 也走同一个 mock 服务，所以总数 = 1 次对话 + 2 次笔记更新。
    assert server.request_count == 3, (
        f"E6 预期 1 次对话 + 2 次退出笔记更新 = 3 次请求（拦截轮无请求），"
        f"实际 {server.request_count} 次"
    )
    assert "Traceback" not in out


def test_warn_threshold_still_sends():
    # 历史约 85% → 打印 80% 提醒但仍发送
    medium = "测" * 170000
    lines = [medium, "继续", "/exit"]
    proc, server = _run_cli(lines, model="gpt-4o-mini")
    out = proc.stdout

    assert "已达 80%" in out, "WARN 阈值未打印提醒"
    assert "本次请求未发送" not in out, "WARN 阈值不应拦截发送"
    # v8：总数 = 2 次对话 + 2 次退出笔记更新（WARN 轮未被拦截）
    assert server.request_count == 4, f"WARN 下应发送 2 次对话 + 2 次笔记，实际 {server.request_count}"
    assert "Traceback" not in out


def test_e4_missing_credential_exits_2():
    env = dict(os.environ)
    env.pop("EIKOCODE_ANTHROPIC_API_KEY", None)
    env.pop("EIKOCODE_OPENAI_API_KEY", None)
    env.pop("EIKOCODE_OPENAI_BASE_URL", None)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "eikocode"],
        input="",
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"E4 退出码应为 2，实际 {proc.returncode}"
    assert "未找到 API Key" in out, "E4 应提示缺少凭据"
    assert "Traceback" not in out
