"""Hook 引擎（v11）：事件 → 条件 → 动作 的求值中枢。

- `evaluate`：同步入口。tool_before 上动作失败 = 拦截（返回拒绝原因），
  其余事件动作失败只记通知（错误隔离），绝不中断主流程。
- `inject` / `observe_async`：供运行时在只读节点投递事件。
- 动作执行对用户终端可见（notice），不静默操控模型。
"""

from __future__ import annotations

from typing import Callable

from .actions import build_context, execute_action, fire_async
from .conditions import evaluate_conditions
from .models import HookSpec, INTERCEPTABLE_EVENTS


class HookEngine:
    """规则集合 + once 状态 + 注入队列。"""

    def __init__(
        self,
        specs: list[HookSpec],
        notice: Callable[[str], None],
        inject: Callable[[str], None] | None = None,
        cwd: str = "",
    ) -> None:
        self.specs = specs
        self.errors: list[str] = []
        self._notice = notice
        self._cwd = cwd
        self._fired_once: set[int] = set()  # once 规则（按 id(spec) 记账）
        self.pending_injections: list[str] = []  # prompt 动作投递的待注入文本

    # -- 入口 ---------------------------------------------------------------- #
    def evaluate(self, event: str, context: dict) -> str | None:
        """同步求值一个事件。返回拦截原因（仅 tool_before 可能），否则 None。

        任何规则失败都不抛异常——tool_before 失败转为拦截原因，
        其余事件失败只记通知（错误隔离）。
        """
        reason: str | None = None
        for spec in self.specs:
            if spec.event != event:
                continue
            if not evaluate_conditions(spec.conditions, context):
                continue
            key = id(spec)
            if spec.once:
                if key in self._fired_once:
                    continue
                self._fired_once.add(key)  # 触发即记账（防并发重复执行）

            if spec.async_ and spec.event not in INTERCEPTABLE_EVENTS:
                self._notice(f"Hook（异步）触发：{spec.event} #{spec.index}")
                fire_async(spec.action, context, spec.timeout, self._notice, self._deliver)
                continue

            result = execute_action(
                spec.action, context, spec.timeout, self._notice, self._deliver
            )
            if spec.event in INTERCEPTABLE_EVENTS:
                if not result.success:
                    if reason is None:  # 首个拦截规则生效
                        reason = f"Hook 拦截（{spec.source} #{spec.index}）：{result.reason}"
                        self._notice(f"已拦截：{reason}")
                else:
                    self._notice(f"Hook 通过：{spec.event} #{spec.index}")
            else:
                if not result.success:
                    self._notice(
                        f"Hook 动作失败（已忽略，不中断主流程）：{spec.event} #{spec.index} {result.reason}"
                    )
        return reason

    # -- 便捷投递 ------------------------------------------------------------ #
    def observe(self, event: str, **context) -> str | None:
        """构造上下文并同步求值（工具 / 消息 / 轮次 / 会话节点用）。"""
        return self.evaluate(event, build_context(event, cwd=self._cwd, **context))

    def _deliver(self, text: str) -> None:
        """prompt 动作的注入投递：包装系统标签后排队（表明系统授权，避免被
        模型当作 prompt injection 拒绝）。"""
        self.pending_injections.append(f"<system-reminder>{text}</system-reminder>")

    def drain_injections(self) -> list[str]:
        """取出待注入文本（运行时在装配请求时调用）。"""
        out, self.pending_injections = self.pending_injections, []
        return out

    def shutdown(self) -> None:
        """会话结束 / 退出事件。"""
        self.observe("shutdown")
