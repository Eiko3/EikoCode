"""代理运行时：ReAct 循环 + 事件流 + 批处理 + plan-only + 可取消。

v3 在 v2 工具系统之上引入。本模块不触碰呈现：它只产出 `AgentEvent`，
由 CLI / 未来的 TUI 订阅渲染。权限拦截位沿用 v2（`permission.request`），
不新增任何权限规则（见 `spec.md` §6「v3 本章明确不做」）。

执行模型：同步 + 迭代通道。循环是一个生成器（`run_turn`），逐事件 yield 出来，
上层在主线程内联消费；取消用 `CancelToken` 标志在轮次 / 批次边界查询。
不引 asyncio，复用 v2 的 `provider.stream` 与执行器线程。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import count
from typing import Callable, Iterator, Sequence

from ..context import (
    BLOCK_MESSAGE,
    WARN_MESSAGE,
    Level,
    estimate_messages,
    level_for,
)
from ..config import Config
from ..context import (
    COMPACT_RATIO,
    CompactBreaker,
    apply_prevention,
    compact_history,
)
from ..errors import INTERRUPT_MESSAGE, EikoCodeError
from ..prompts import (
    PLAN_FULL_INTERVAL,
    assemble,
    capture,
    format_message,
    plan_reminder,
)
from ..providers import GenerateParams, Role, select_provider
from ..providers.base import (
    BACKOFF_SECONDS,
    MAX_RETRIES,
    Message,
    Thinking,
    ToolCall,
    UsageInfo,
)
from ..security import ACTION_DENY, SecurityPipeline
from ..security.rules import Rule
from ..session import Session
from ..tools import execute_tool
from ..tools.base import PermissionLevel
from .events import AgentEvent, EventKind

# 最大轮数默认值（见 checklist.md 第 19 组，提议默认 25）。
MAX_TURN = 25

# 工具连续失败上限（见 checklist.md 第 11 组，提议默认 3）。
MAX_TOOL_FAILURES = 3

# 工具结果计入上下文用量后若越过上限：沿用「不静默截断」原则，拒绝发送并提示。
TOOL_RESULT_BLOCK_MESSAGE = (
    "工具结果过大，已阻止发送。请开启新会话（/clear）或让模型使用更精确的工具。"
)


class CancelToken:
    """可在轮次 / 批次边界被查询的取消标志。

    Ctrl+C 与 `/cancel` 都映射到它；循环在每轮开始、每批前后检查，置位即停止。
    """

    def __init__(self) -> None:
        self._flag = False

    def cancel(self) -> None:
        self._flag = True

    def is_cancelled(self) -> bool:
        return self._flag


@dataclass
class _Ui:
    """权限提示与阈值提醒需要落到终端——这是用户交互边界，不是循环过程的呈现。

    CLI 传入其 `Renderer`；运行时只用它的 `error` / `notice` 两类做交互提示，
    循环过程本身一律走 `AgentEvent`。
    """

    error: Callable[[str], None]
    notice: Callable[[str], None]


def _iter_stream(factory: Callable[[], Iterator]) -> Iterator:
    """带递增退避重试地跑一次流式生成（文本 / 工具调用 / 思考共用）。

    只有在一个块都还没产出时才重试——一旦有任何内容吐出，再重试会接出两段
    半截回复。逐块 yield，保证上层实时渲染。逻辑与 `providers.base._run_stream` 一致。
    """
    last: EikoCodeError | None = None
    for attempt in range(MAX_RETRIES + 1):
        produced = False
        try:
            for chunk in factory():
                produced = True
                yield chunk
            return
        except EikoCodeError as exc:
            last = exc
            if produced or not exc.retryable or attempt >= MAX_RETRIES:
                raise
            time.sleep(BACKOFF_SECONDS[attempt])
    if last is not None:
        raise last


class AgentRuntime:
    """ReAct 代理运行时。

    一个 `AgentRuntime` 绑定一次会话（Session）、工具注册表与配置；模型可中途切换。
    一次用户轮次调用 `run_turn(text, cancel)`，它产出该轮的全部事件，直到模型不再
    请求工具（或达到最大轮数 / 被取消）。
    """

    def __init__(
        self,
        config,
        session: Session,
        registry,
        ask: Callable[[str], str],
        ui: _Ui,
        model: str,
        instructions: str = "",
        archiver=None,
        notes=None,
        skills=None,
        hooks=None,
    ) -> None:
        self.config = config
        self.session = session
        self.registry = registry
        self._ask = ask
        self._ui = ui
        self._model = model
        self._plan_only = False
        # v5 安全决策流水：会话临时规则列表由流水与人回路共享（就地追加）。
        self._session_rules: list[Rule] = []
        self._pipeline = SecurityPipeline(config, self._session_rules)
        # v4 装配状态：稳定前缀之外的「变化内容」都在这里。
        self._last_usage: UsageInfo | None = None
        # 首条快照（会话内不变，/clear 后重建）与上次探测快照（变化检测用）必须
        # 分开——若用同一个变量，变更时会改写首条，违反「不改写已发出历史」。
        self._env_first = None
        self._env_last = None
        self._plan_turns = 0  # 只规划模式开启以来经过的轮数（节奏注入用）
        # v7 压缩状态：熔断器跨轮持续，摘要模型缺省用当前会话模型
        self._breaker = CompactBreaker()
        # v8：项目指令（会话内固定，注入对话最早位置）+ 存档 + 笔记
        self._instructions = instructions or ""
        self.archiver = archiver
        self.notes = notes
        # v10：Skill 管理器（激活 SOP 钉在装配环境上下文段；None = 未启用）
        self.skills = skills
        # v11 Hook 引擎（None = 未启用）；_session_started 支撑会话开始一次性触发
        self.hooks = hooks
        self._session_started = False
        # v12：外部注入队列（后台任务完成通知等）+ 角色轮数上限覆盖
        self._extra_injections: list[str] = []
        self._max_turns_override: int | None = None

    # -- 开关 --------------------------------------------------------------- #
    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model: str) -> None:
        self._model = model

    @property
    def plan_only(self) -> bool:
        return self._plan_only

    def set_plan_only(self, value: bool) -> None:
        self._plan_only = bool(value)
        self._plan_turns = 0  # 开关状态变化时重新计节奏

    @property
    def auto_approve(self) -> bool:
        """v4 自动批准（v5 起为放行档的别名）。"""
        return self._pipeline.mode == "permissive"

    def set_auto_approve(self, value: bool) -> None:
        self._pipeline.set_mode("permissive" if value else "default")

    @property
    def mode(self) -> str:
        """当前权限档位（strict / default / permissive）。"""
        return self._pipeline.mode

    def set_mode(self, mode: str) -> None:
        self._pipeline.set_mode(mode)

    @property
    def last_usage(self) -> UsageInfo | None:
        """最近一次响应的归一用量（含缓存字段）；尚无响应时为 None。"""
        return self._last_usage

    # -- 请求装配（v4）------------------------------------------------------ #
    def _shell_cwd(self) -> str | None:
        """Shell 工具延续的会话内 cwd；真实工作目录只存在于工具实例中。"""
        shell = self.registry.get("Shell")
        if shell is not None and hasattr(shell, "current_cwd"):
            return str(shell.current_cwd())
        return None

    def _environment_messages(self) -> tuple[str, str | None]:
        """环境首条消息 + 变更追加消息（无变更时为 None）。

        首条在会话生命周期内保持不变；变化一律以「追加新条」表达，不改写
        任何已发出的消息（缓存前缀不因此破坏）。`_env_first` 为空表示
        新会话（含 /clear 后），按当前环境重建首条。
        """
        snap = capture(self._shell_cwd())
        extra: str | None = None
        if self._env_first is None:
            self._env_first = snap
            self._env_last = snap
        elif snap != self._env_last:
            extra = format_message(snap)
            self._env_last = snap
            self._ui.notice("环境变化：工作目录或日期已变更，本轮请求附带最新环境信息。")
        return format_message(self._env_first), extra

    def _plan_injections(self) -> list[str]:
        """按节奏生成只规划模式提醒（见 checklist.md 组 30）。

        注入经 `_Ui.notice` 对用户可见——不静默操控模型。
        """
        if not self._plan_only:
            return []
        self._plan_turns += 1
        full = self._plan_turns == 1 or self._plan_turns % PLAN_FULL_INTERVAL == 1
        self._ui.notice(
            f"已注入：模式提醒（{'完整' if full else '精简'}，第 {self._plan_turns} 轮）。"
        )
        return [plan_reminder(self._plan_turns)]

    def _build_request(
        self, env_first: str, env_extra: str | None, injections: Sequence[str]
    ) -> list[Message]:
        """把稳定前缀之外的内容拼进当轮请求（不写会话历史）。

        顺序：环境首条 → 会话历史 → 环境追加 → 当轮注入。
        """
        msgs: list[Message] = []
        # v8：项目指令最早（用户级在前、项目级在后由加载器拼好），会话内固定
        if self._instructions:
            msgs.append(Message(role=Role.USER, content=self._instructions))
        # v10：Skill 环境上下文段（清单 + 已激活 SOP），先于环境首条。
        # 激活是显式动作（load_skill / 短命令），变化经由通知对用户可见。
        if self.skills is not None:
            pinned = self.skills.pinned_text()
            if pinned:
                msgs.append(Message(role=Role.USER, content=pinned))
        msgs.append(Message(role=Role.USER, content=env_first))
        msgs.extend(self.session.messages())
        if env_extra:
            msgs.append(Message(role=Role.USER, content=env_extra))
        for text in injections:
            msgs.append(Message(role=Role.USER, content=text))
        return msgs

    # -- 上下文压缩（v7）----------------------------------------------------- #
    # -- 会话存档（v8）------------------------------------------------------- #
    def _archive(self, message: Message) -> None:
        if self.archiver is not None:
            self.archiver.append(message)

    def restore_session(self, messages: list[Message], archiver=None, stale_notice: str | None = None) -> None:
        """恢复历史会话（v8）：整体替换历史并可选换绑存档。

        时间跨度提醒作为一条系统提示消息追加到会话末尾（进对话、不归档），
        模型据此知晓上下文存在时间断层。
        """
        self.session.rewrite(messages)
        if stale_notice:
            self.session.add_user(stale_notice)
        if archiver is not None:
            self.archiver = archiver
        if stale_notice:
            self._ui.notice(stale_notice)

    def _compact_if_needed(self) -> None:
        """自动兜底：累计用量达到 90% 时生成摘要替换较早轮次。

        总开关（auto_compact=false）在这里统一拦截；熔断状态下静默跳过
        （熔断告知已在失败时输出过）；摘要模型不可用也只提示并跳过，不阻断本轮。
        """
        from ..providers import select_provider  # 局部导入避免启动期拖 SDK

        if not bool(getattr(self.config, "auto_compact", True)):
            return  # 开关关闭：回到「80% 提醒 + 95% 拒绝」旧行为
        used = estimate_messages(self.session.messages())
        if used / self.config.context_limit < COMPACT_RATIO:
            return
        if self._breaker.open:
            return
        model = str(getattr(self.config, "summary_model", "") or "") or self.config.model
        try:
            provider = select_provider(self.config, model)
        except EikoCodeError as exc:
            self._ui.notice(f"摘要模型不可用（{model}）：{exc.user_message}；本次跳过自动压缩")
            return
        compact_history(
            self.session, provider, model, self._ui.notice, self._breaker
        )
        # v11 Hook：压缩完成事件（无论是否实际压缩，触发点即兜底流程走完）
        if self.hooks is not None:
            self.hooks.observe("compaction")

    def manual_compact(self) -> bool:
        """手动触发兜底压缩（/compact）：绕过熔断与阈值，失败计数照记。"""
        model = str(getattr(self.config, "summary_model", "") or "") or self.config.model
        provider = select_provider(self.config, model)
        return compact_history(
            self.session, provider, model, self._ui.notice, self._breaker, force=True
        )

    @property
    def compact_breaker_open(self) -> bool:
        return self._breaker.open

    # -- Hook 系统（v11）----------------------------------------------------- #
    @property
    def instructions(self) -> str:
        """项目指令文本（v8；子工作者 Fork 时继承用）。"""
        return self._instructions

    def push_injection(self, text: str) -> None:
        """外部投递注入文本（v12 后台任务完成通知等），下一轮请求生效。"""
        self._extra_injections.append(text)

    def _hook_error(self, message: str) -> None:
        """error 事件节点：通知 Hook 引擎（动作失败只记日志，不中断）。"""
        if self.hooks is not None:
            self.hooks.observe("error", error=message)

    # -- 阈值（沿用 v2 上下文层）------------------------------------------- #
    def _at_warn(self) -> bool:
        used = estimate_messages(self.session.messages())
        return level_for(used, self.config.context_limit) is Level.WARN

    def _over_limit(self, message: str) -> bool:
        used = estimate_messages(self.session.messages())
        if level_for(used, self.config.context_limit) is Level.BLOCK:
            self._ui.error(message)
            return True
        return False

    # -- 工具执行（单工具，沿用 v2 执行器）---------------------------------- #
    def _is_read(self, tc: ToolCall) -> bool:
        tool = self.registry.get(tc.name)
        return tool is not None and tool.permission is PermissionLevel.READ

    def _run_one(self, tc: ToolCall) -> tuple[str, bool]:
        """执行一次工具调用。返回（结果文本, 是否算作失败）。"""
        tool = self.registry.get(tc.name)
        if tool is None:
            return f"未知工具：{tc.name}", True

        # plan-only：写 / 执行类工具不进权限确认，直接以拦截结果回写并继续跑读。
        if self._plan_only and tool.permission is not PermissionLevel.READ:
            return "plan-only 已拦截：当前为只规划模式，未执行写 / 执行类工具。", False

        # v5 安全决策流水：黑名单 → 沙箱 → 规则 → 档位 → 人在回路。
        # 拒绝回写 reason 本身（「用户拒绝执行」/「路径越界：…」等），保持 v2 文案兼容。
        decision = self._pipeline.evaluate(tool, tc.arguments, self._ui, self._ask)
        if decision.action == ACTION_DENY:
            if not decision.silent:
                self._ui.notice(f"已拦截：{decision.reason}")
            return decision.reason, False
        if not decision.silent:
            self._ui.notice(f"已放行：{decision.reason}")

        # v11 Hook：工具执行前（同步、可拦截）。挂在 v5 流水之后——
        # 已知高危先由流水裁决，Hook 再叠加用户自定义的细粒度策略；
        # 拦截原因作为工具结果回写，Agent 据此调整策略（拦截循环）。
        if self.hooks is not None:
            hook_reason = self.hooks.observe(
                "tool_before", tool=tool.name, args=tc.arguments
            )
            if hook_reason:
                return hook_reason, False

        try:
            return execute_tool(tool, tc.arguments), False
        except EikoCodeError as exc:
            return f"工具执行出错：{exc.user_message}", True
        except Exception as exc:  # 双保险
            return f"工具执行出错：{type(exc).__name__}", True

    def _execute_batch(
        self, tool_calls: Sequence[ToolCall], cancel: CancelToken
    ) -> dict[str, tuple[str, bool]]:
        """分批调度：读组并发、写组串行（读组整体完成后再写组）。

        - 组内单个失败不影响同组其他工具，结果各自回写。
        - 写组某工具失败后中止本批剩余写调用。
        - 每次单工具执行仍走既有超时与错误归一。
        """
        read_calls = [tc for tc in tool_calls if self._is_read(tc)]
        write_calls = [tc for tc in tool_calls if not self._is_read(tc)]
        results: dict[str, tuple[str, bool]] = {}

        # 读组并发（线程池）。每组至少 1 个工具，max_workers 不超过组大小。
        if read_calls:
            with ThreadPoolExecutor(max_workers=max(1, len(read_calls))) as ex:
                futures = {ex.submit(self._run_one, tc): tc for tc in read_calls}
                for fut in as_completed(futures):
                    tc = futures[fut]
                    results[tc.id] = fut.result()

        # 读组全部完成后，写组串行（响应顺序）。
        if write_calls:
            for tc in write_calls:
                if cancel.is_cancelled():
                    results[tc.id] = ("（已取消）", True)
                    continue
                res = self._run_one(tc)
                results[tc.id] = res
                if res[1]:  # 写组某失败 → 中止本批剩余写
                    idx = write_calls.index(tc)
                    for rest in write_calls[idx + 1 :]:
                        results[rest.id] = (
                            "（本批后续写工具已中止：前面有写工具失败）",
                            True,
                        )
                    break

        return results

    # -- 主循环 ------------------------------------------------------------- #
    def run_turn(self, text: str, cancel: CancelToken) -> Iterator[AgentEvent]:
        """跑一轮用户请求，逐事件吐出循环过程。

        循环：装配（含压缩）→ 调模型 → 收响应 → 含工具调用则分批执行回填 →
        下一轮；模型不再请求工具即终止。终止情形：无工具调用 / 达最大轮数 /
        用户取消。
        """
        # v11 Hook：会话开始（run_turn 首轮触发一次）
        if self.hooks is not None and not self._session_started:
            self._session_started = True
            self.hooks.observe("session_start", message=text)

        # v11 Hook：轮次开始
        if self.hooks is not None:
            self.hooks.observe("turn_start", message=text)

        # v7 兜底压缩：请求前先看累计用量（预防层已在工具结果回写时就地完成）。
        # 压缩只动会话历史，稳定前缀 / 环境消息 / 注入不受影响。
        if bool(getattr(self.config, "auto_compact", True)):
            self._compact_if_needed()

        yield AgentEvent.user_message(text)

        # 阈值在发出网络请求之前判定（此时不含本轮用户消息）。
        if self._over_limit(BLOCK_MESSAGE):
            return

        if self._at_warn():
            self._ui.notice(WARN_MESSAGE)

        # turn_start 记录本轮回话开始前的消息数，用于整轮回滚（不保留用户消息）。
        # 会话为空（含 /clear 后）时重置环境首条：下轮按当前环境重建，而非追加。
        if self.session.message_count == 0:
            self._env_first = None
        turn_start = self.session.message_count
        user_message = Message(role=Role.USER, content=text)
        self.session.add_user(text)
        self._archive(user_message)

        # 稳定前缀：会话内逐字节不变（v4，缓存根基）。模块是常量，
        # 这里装配一次，循环内每轮复用同一文本。
        system_text = assemble()

        try:
            consecutive_failures = 0
            tool_results_added = False  # 决定后续阈值拦截用哪种文案
            for turn in count(1):
                if cancel.is_cancelled():
                    self.session.truncate(turn_start)
                    self._hook_error(INTERRUPT_MESSAGE)
                    yield AgentEvent.error(INTERRUPT_MESSAGE)
                    return

                if turn > (self._max_turns_override or MAX_TURN):
                    self.session.truncate(turn_start)
                    self._hook_error(f"已达到最大轮数上限")
                    yield AgentEvent.error(
                        f"已达到最大轮数上限，已停止本轮以避免无限循环。"
                    )
                    return

                # 首轮（仅用户消息）超限用通用拦截提示；回写工具结果后超限用专用提示。
                block_msg = TOOL_RESULT_BLOCK_MESSAGE if tool_results_added else BLOCK_MESSAGE
                if self._over_limit(block_msg):
                    self.session.truncate(turn_start)
                    return

                collected_text: list[str] = []
                tool_calls: list[ToolCall] = []
                provider = select_provider(self.config, self._model)
                params = GenerateParams(
                    model=self._model,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    system=system_text,
                )
                tools = [t.spec() for t in self.registry.all()]

                # v4 装配：环境首条 → 历史 → 环境追加 → 当轮注入。
                # 环境追加与注入只进请求，不写会话历史；变化只追加不改写。
                env_first, env_extra = self._environment_messages()
                # v11 Hook：消息发送前；prompt 动作的注入排队下一轮生效
                injections = self._plan_injections()
                if self.hooks is not None:
                    self.hooks.observe("message_send", message=text)
                    injections += self.hooks.drain_injections()
                # v12：外部注入（后台任务完成通知等）
                if self._extra_injections:
                    injections += self._extra_injections
                    self._extra_injections = []
                request_messages = self._build_request(
                    env_first, env_extra, injections
                )

                try:
                    for item in _iter_stream(
                        lambda: provider.stream(request_messages, params, tools)
                    ):
                        if isinstance(item, str):
                            collected_text.append(item)
                            yield AgentEvent.text_delta(item)
                        elif isinstance(item, Thinking):
                            if item.text:  # 空思考不刷屏
                                yield AgentEvent.thinking(item.text)
                        elif isinstance(item, UsageInfo):
                            self._last_usage = item
                        else:
                            tool_calls.append(item)
                except KeyboardInterrupt:
                    # Ctrl+C 映射到取消：整轮回滚，不产生半截回复。
                    self.session.truncate(turn_start)
                    self._hook_error(INTERRUPT_MESSAGE)
                    yield AgentEvent.error(INTERRUPT_MESSAGE)
                    return
                except EikoCodeError as exc:
                    self.session.truncate(turn_start)
                    self._hook_error(exc.user_message)
                    yield AgentEvent.error(exc.user_message)
                    return

                if tool_calls:
                    # 先把带工具调用的助手消息入库（供应商续生成依赖它），再分批回写结果。
                    assistant_message = Message(
                        role=Role.ASSISTANT,
                        content="".join(collected_text),
                        tool_calls=tuple(tool_calls),
                    )
                    self.session.add_assistant_with_tools(
                        "".join(collected_text), tuple(tool_calls)
                    )
                    self._archive(assistant_message)
                    for tc in tool_calls:
                        yield AgentEvent.tool_call_start(tc.name)
                    results = self._execute_batch(tool_calls, cancel)
                    # v7 预防层：超大工具结果就地存盘留预览（回写之前）
                    results = apply_prevention(
                        results, tool_calls, self.config, self._ui.notice
                    )
                    # 按响应顺序回写结果并发事件。
                    for tc in tool_calls:
                        res, is_error = results[tc.id]
                        self.session.add_tool_result(tc.id, res)
                        self._archive(
                            Message(role=Role.USER, content=res, tool_call_id=tc.id)
                        )
                        # v11 Hook：工具执行后（message = 结果，内部截断预览）
                        if self.hooks is not None:
                            self.hooks.observe(
                                "tool_after", tool=tc.name, args=tc.arguments,
                                message=res, error=("工具执行出错" if is_error else ""),
                            )
                        yield AgentEvent.tool_result(tc.name, res)
                        consecutive_failures = consecutive_failures + 1 if is_error else 0
                    if consecutive_failures > MAX_TOOL_FAILURES:
                        self.session.truncate(turn_start)
                        self._hook_error("工具连续失败超过 3 次")
                        yield AgentEvent.error(
                            "工具连续失败超过 3 次，已停止本轮；请检查命令或参数后重试。"
                        )
                        return
                    tool_results_added = True
                    continue

                final = "".join(collected_text)
                self.session.complete_assistant(final)
                self._archive(Message(role=Role.ASSISTANT, content=final))
                # v11 Hook：响应接收后 + 轮次结束
                if self.hooks is not None:
                    self.hooks.observe("message_receive", message=final)
                yield AgentEvent.final_reply(final)
                if self.hooks is not None:
                    self.hooks.observe("turn_end", message=final)
                # v8：对话轮结束——轮次计数与笔记触发检查
                if self.notes is not None:
                    self.notes.on_turn_end(self.session)
                return
        except KeyboardInterrupt:
            # 工具执行期间的 Ctrl+C 已在上方捕获；这里兜底。
            self.session.truncate(turn_start)
            self._hook_error(INTERRUPT_MESSAGE)
            yield AgentEvent.error(INTERRUPT_MESSAGE)
            return
