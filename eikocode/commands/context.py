"""界面控制接口与命令上下文（v9）。

命令处理函数只拿 `CommandContext`：它集中携带会话、运行时、配置等依赖，
并把「显示消息、查询用量、刷新状态」等终端操作收敛为一组方法——命令
实现因此不 import 渲染器与 CLI 内部符号（spec.md §3 能力 69）。
"""

from __future__ import annotations

from ..context import estimate_messages
from ..session import Session


class CommandContext:
    """命令执行时注入的依赖容器 + 界面控制接口。

    命令处理函数签名统一为 `handler(ctx, arg)`；所有终端交互经由这里的
    方法，命令本体不感知 Rich / 渲染器的存在。
    """

    def __init__(
        self,
        session: Session,
        runtime,
        config,
        cancel,
        renderer,
        mcp=None,
        notes=None,
        command_registry=None,
        skills=None,
        tasks=None,
        worktrees=None,
        team=None,
    ) -> None:
        self.session = session
        self.runtime = runtime
        self.config = config
        self.cancel = cancel
        self.mcp = mcp
        self.notes = notes
        self.command_registry = command_registry
        self.skills = skills  # v10 SkillManager（鸭子类型，避免反向依赖）
        self.tasks = tasks  # v12 TaskManager
        self.worktrees = worktrees  # v13 WorktreeManager
        self.team = team  # v14 TeamCoordinator
        self.worktrees = worktrees  # v13 WorktreeManager
        self.team = team  # v14 TeamCoordinator
        self._renderer = renderer

    # -- 界面控制接口 -------------------------------------------------------- #
    def notify(self, text: str) -> None:
        """显示一条系统通知（强调）。"""
        self._renderer.notice(text)

    def info(self, text: str) -> None:
        """显示一条普通信息。"""
        self._renderer.info(text)

    def error(self, text: str) -> None:
        """显示一条错误。"""
        self._renderer.error(text)

    def show_help(self, specs) -> None:
        """渲染命令帮助列表（隐藏命令不出现），含别名与描述。"""
        self._renderer.info("可用命令：")
        for spec in specs:
            alias = f"（别名：{' '.join(spec.aliases)}）" if spec.aliases else ""
            self._renderer.info(f"  {spec.usage}  {spec.description}{alias}")

    def show_usage(self) -> None:
        """查询用量（token 估算 + 缓存命中）。"""
        usage_info = self.runtime.last_usage
        cache = (usage_info.cache_hit, usage_info.input_tokens) if usage_info else None
        self._renderer.usage(
            estimate_messages(self.session.messages()),
            self.config.context_limit,
            self.session.turns,
            cache,
        )

    def usage_percent(self) -> int:
        used = estimate_messages(self.session.messages())
        if self.config.context_limit <= 0:
            return 100
        return round(used / self.config.context_limit * 100)

    def status_segment(self) -> str:
        """提示符状态段：当前模式与用量占比（提议默认，见 checklist 组 72）。"""
        if self.runtime.plan_only:
            mode = "计划"
        else:
            mode = {
                "strict": "严格",
                "default": "执行",
                "permissive": "放行",
            }.get(self.runtime.mode, self.runtime.mode)
        return f"{mode}·{self.usage_percent()}%"
