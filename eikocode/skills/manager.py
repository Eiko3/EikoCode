"""load_skill 工具（v10）：系统级内置工具，两阶段加载的第二阶段。

模型判断需要某个 Skill 时调用本工具；不受任何白名单约束（Skill 可嵌套
触发）。共享模式激活 = SOP 钉进装配的环境上下文段 + 工具集收窄；隔离
模式激活 = 立即在独立会话执行并把最终回复作为工具结果回流。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..commands.registry import CommandKind, CommandSpec
from ..tools.base import Tool
from .loader import _materialize_dir_tools
from .models import MODE_SHARED, SkillParseError, SkillSpec, parse_skill
from .whitelist import WhitelistView

# 隔离模式嵌套深度上限（提议默认 2，见 checklist 组 78）：隔离会话里再触发
# 隔离 Skill 属合法嵌套，但每层都是一次真实执行，必须设界。
MAX_NESTED_ISOLATION = 2


class LoadSkillTool(Tool):
    """加载 Skill 的系统级工具。"""

    name = "load_skill"
    description = (
        "加载并激活一个 Skill：把它的标准作业程序（SOP）置入当前上下文，"
        "并按其声明收窄可用工具集（隔离模式 Skill 则立即在独立会话执行并返回结果）。"
        "调用前请参考系统提示中的可用 Skill 清单。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要加载的 Skill 名字"},
            "task": {"type": "string", "description": "本次要用该 Skill 完成的任务（可空）"},
        },
        "required": ["name"],
    }

    def __init__(self, on_load: Callable[[str, str], str]) -> None:
        self._on_load = on_load

    def execute(self, arguments: dict) -> str:
        name = str(arguments.get("name") or "").strip()
        task = str(arguments.get("task") or "").strip()
        if not name:
            return "错误：缺少 name 参数。"
        return self._on_load(name, task)


class SkillManager:
    """Skill 全生命周期：发现 → 清单注入 → 激活 → 钉住 / 收窄 / 短命令。"""

    def __init__(
        self,
        specs: list[SkillSpec],
        errors: list[str],
        base_registry,
        notice: Callable[[str], None],
        command_registry=None,
    ) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self.errors = errors
        self._base = base_registry
        self._notice = notice
        self._command_registry = command_registry
        self.activated: list[SkillSpec] = []
        self._runtime = None
        self._iso_depth = 0  # 隔离嵌套深度
        self._register_dir_tools(specs)

    # -- 构建与绑定 ---------------------------------------------------------- #
    @classmethod
    def load(
        cls,
        base_registry,
        notice: Callable[[str], None],
        command_registry=None,
        project_dir: Path | None = None,
        user_dir: Path | None = None,
    ) -> "SkillManager":
        """启动链入口：三级发现 + fail-fast + 注册系统级 load_skill。"""
        known = {t.name for t in base_registry.all()}
        specs, errors = _discover(project_dir, user_dir, known)
        manager = cls(specs, errors, base_registry, notice, command_registry)
        base_registry.register(LoadSkillTool(manager.activate))
        for err in errors:
            notice(f"Skill 加载失败：{err}")
        if specs:
            notice(f"已加载 {len(specs)} 个 Skill（/skills 查看清单）")
        return manager

    def bind(self, runtime) -> None:
        """挂到运行时：装配链取钉住文本，工具集收窄经 runtime.registry 换视图。"""
        self._runtime = runtime
        runtime.skills = self

    def _register_dir_tools(self, specs: list[SkillSpec]) -> None:
        for spec in specs:
            for tool in _materialize_dir_tools(spec):
                self._base.register(tool)

    # -- 清单与钉住（装配链消费）--------------------------------------------- #
    @property
    def specs(self) -> dict[str, SkillSpec]:
        return dict(self._specs)

    def catalog_text(self) -> str:
        """启动清单：名字 + 一句话说明。会话内固定，两次装配逐字节一致。"""
        if not self._specs:
            return ""
        lines = [
            "【可用 Skill 清单】需要时调用 load_skill 工具激活（name + task）。",
        ]
        for spec in self._specs.values():
            mode = "隔离" if spec.is_isolated else "共享"
            lines.append(f"- {spec.name}：{spec.description}（{mode}模式）")
        return "\n".join(lines)

    def pinned_text(self) -> str:
        """环境上下文段：清单 + 全部已激活 SOP（每轮装配都在，先于环境首条）。"""
        parts = []
        catalog = self.catalog_text()
        if catalog:
            parts.append(catalog)
        for spec in self.activated:
            parts.append(f"【已激活 Skill：{spec.name}】以下 SOP 必须遵循：\n{spec.sop}")
        return "\n\n".join(parts)

    # -- 激活 ---------------------------------------------------------------- #
    def activate(self, name: str, task: str = "") -> str:
        """load_skill 工具入口。返回文本作为工具结果回写主对话。"""
        spec = self._specs.get(name)
        if spec is None:
            available = ", ".join(self._specs) or "（无）"
            return f"未知 Skill：{name}。可用：{available}"
        spec = self._refresh(spec)  # 热更新：激活时重读源文件

        if not spec.is_isolated:
            return self._activate_shared(spec)
        # 隔离模式：立即在独立会话执行（不影响主会话激活列表与白名单）。
        # 嵌套护栏：隔离会话内的模型再调 load_skill 触发隔离时受深度限制，
        # 否则一次嵌套就是几分钟的真实执行、无界递归。
        if self._iso_depth >= MAX_NESTED_ISOLATION:
            return (
                f"已达到隔离模式嵌套上限（{MAX_NESTED_ISOLATION} 层）："
                "请在当前会话直接执行任务，不要再触发隔离 Skill。"
            )
        from .runner import run_isolated

        main_messages = self._runtime.session.messages() if self._runtime else ()
        self._iso_depth += 1
        try:
            return run_isolated(
                spec,
                task,
                self._runtime.config if self._runtime else None,
                self._base,
                self._runtime._ask if self._runtime else (lambda p: "y"),
                self._runtime._ui if self._runtime else None,
                main_messages,
                self._build_sub_registry,
            )
        finally:
            self._iso_depth -= 1

    def _refresh(self, spec: SkillSpec) -> SkillSpec:
        """文件型 Skill 重读源文件（热更新）；内置与读取失败沿用旧内容。"""
        if spec.path is None:
            return spec
        try:
            fresh = parse_skill(spec.path.read_text(encoding="utf-8", errors="replace"), spec.source)
        except (OSError, SkillParseError):
            return spec
        fresh.path = spec.path
        fresh.dir_tools, fresh.impl_path = spec.dir_tools, spec.impl_path
        self._specs[spec.name] = fresh
        # 同步激活列表中的引用
        self.activated = [fresh if s.name == spec.name else s for s in self.activated]
        return fresh

    def _activate_shared(self, spec: SkillSpec) -> str:
        already = any(s.name == spec.name for s in self.activated)
        if not already:
            self.activated.append(spec)
        self._apply_whitelist()
        self._register_short_command(spec)
        tool_note = (
            f"工具集已收窄至白名单：{', '.join(spec.tools)}" if spec.tools else "工具集未收窄"
        )
        if spec.tools:
            self._notice(f"已激活 Skill「{spec.name}」：{tool_note}；缓存前缀本次失效。")
        else:
            self._notice(f"已激活 Skill「{spec.name}」（SOP 已钉入上下文）。")
        return (
            f"Skill「{spec.name}」已激活。SOP 已钉入上下文（每轮可见），{tool_note}。"
            "请现在按 SOP 执行用户任务。"
        )

    def _apply_whitelist(self) -> None:
        """收窄 = 各已激活 Skill 白名单的并集；任一 Skill 未声明白名单则不收窄。"""
        if self._runtime is None:
            return
        declaring = [s for s in self.activated if s.tools]
        if not declaring:
            self._runtime.registry = self._base
            return
        allowed: set[str] = set()
        for s in declaring:
            allowed.update(s.tools)
        self._runtime.registry = WhitelistView(self._base, allowed)

    def _build_sub_registry(self, spec: SkillSpec):
        """隔离模式的独立注册表：白名单内工具 + 目录型工具 + 系统级 load_skill。"""
        from ..tools import ToolRegistry
        from .whitelist import WhitelistView

        sub = ToolRegistry()
        for tool in self._base.all():
            if tool.name == "load_skill":
                continue  # 子注册表统一挂自己的系统级入口
            sub.register(tool)
        sub.register(LoadSkillTool(self.activate))
        if spec.tools:
            return WhitelistView(sub, set(spec.tools))
        return sub

    # -- 短命令 --------------------------------------------------------------- #
    def _register_short_command(self, spec: SkillSpec) -> None:
        if self._command_registry is None:
            return
        cmd_name = f"/{spec.name}"
        if self._command_registry.get(cmd_name) is not None:
            return
        self._command_registry.register(
            CommandSpec(
                name=cmd_name,
                description=f"Skill：{spec.description}",
                usage=f"{cmd_name} [任务]",
                kind=CommandKind.PROMPT,
                handler=self._make_short_handler(spec.name),
                param_hint="[任务]",
            )
        )

    def _make_short_handler(self, name: str):
        def handler(ctx, arg: str):
            from ..commands.builtin import CommandResult

            spec = self._refresh(self._specs[name])  # 执行时重读源文件 = 热更新
            if spec.is_isolated:
                # 隔离 Skill：让模型经 load_skill 触发独立执行（结果回流对话）
                task = f"：{arg}" if arg else ""
                return CommandResult(
                    prompt_text=f"请调用 load_skill 工具加载 Skill「{name}」并执行任务{task or '（自行判断）'}。"
                )
            task = f"任务：{arg}" if arg else "请按 SOP 处理当前上下文中最相关的事项。"
            return CommandResult(prompt_text=f"按已激活的 {name} Skill 的 SOP 执行。{task}")

        return handler

    # -- 清空 / 重建 ----------------------------------------------------------- #
    def deactivate_all(self) -> None:
        """清空已激活 Skill、恢复工具集、注销短命令（/clear 与 /skills reload 用）。"""
        self.activated.clear()
        if self._runtime is not None:
            self._runtime.registry = self._base
        if self._command_registry is not None:
            for name in [f"/{s.name}" for s in self._specs.values()]:
                self._command_registry.unregister(name)

    def reload(self) -> None:
        """强制重新扫描：激活列表清空，需重新激活（/skills reload）。"""
        self.deactivate_all()
        project = Path.cwd() / ".eikocode" / "skills"
        user = Path.home() / ".eikocode" / "skills"
        known = {t.name for t in self._base.all() if t.name != "load_skill"}
        specs, errors = _discover(project, user, known)
        self._specs = {spec.name: spec for spec in specs}
        self.errors = errors
        for err in errors:
            self._notice(f"Skill 加载失败：{err}")
        self._notice(f"Skill 已重新扫描：{len(specs)} 个可用，激活列表已清空。")

    def status_lines(self) -> list[str]:
        lines = []
        for spec in self._specs.values():
            active = "已激活" if any(s.name == spec.name for s in self.activated) else "未激活"
            mode = "隔离" if spec.is_isolated else "共享"
            lines.append(f"{spec.name}（{mode}·{active}）：{spec.description}  [{spec.source}]")
        if not lines:
            lines.append("没有可用 Skill。")
        return lines


def _discover(project_dir, user_dir, known):
    from .loader import discover_skills

    return discover_skills(project_dir, user_dir, known)
