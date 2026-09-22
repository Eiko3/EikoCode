"""Skill 加载器（v10）：三级发现、同名覆盖、目录型 Skill、fail-fast。

三级优先级（高 → 低）：项目 `.eikocode/skills/` > 用户 `~/.eikocode/skills/`
> 内置常量；同名按优先级覆盖（先到先得，低优先级不再登记）。
单文件型 `<名字>.md` 与目录型 `<名字>/SKILL.md`（可带 `tools/schema.json`
+ `tools/impl.py` 单分发入口）都支持。解析失败的单个文件跳过并记入错误
清单，不阻断整体加载；白名单含当前不存在的工具 → fail-fast 点名拒绝。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from ..tools.base import PermissionLevel, Tool
from .builtin_skills import BUILTIN_SKILLS
from .models import SkillParseError, SkillSpec, parse_skill

SKILLS_DIRNAME = "skills"
SKILL_ENTRY = "SKILL.md"
TOOLS_SCHEMA = "schema.json"
TOOLS_IMPL = "impl.py"


class DirectorySkillTool(Tool):
    """目录型 Skill 的一个工具：实现统一委托给 impl.py 的单分发入口。

    impl.py 需暴露 `execute(tool_name: str, arguments: dict) -> str`。
    权限等级取 schema 声明（缺省按 write 保守处理），走 v5 安全流水。
    """

    def __init__(self, name: str, description: str, parameters: dict, permission: str, func) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func = func
        try:
            self.permission = PermissionLevel(permission)
        except ValueError:
            self.permission = PermissionLevel.WRITE

    def execute(self, arguments: dict) -> str:
        return self._func(self.name, arguments)


def _load_directory_tools(skill_dir: Path, skill_name: str) -> tuple[tuple[dict, ...], Path | None, list[str]]:
    """读取目录型的 tools/schema.json + impl.py。返回（schema 列表, impl 路径, 错误）。"""
    schema_path = skill_dir / "tools" / TOOLS_SCHEMA
    impl_path = skill_dir / "tools" / TOOLS_IMPL
    errors: list[str] = []
    if not schema_path.is_file() and not impl_path.is_file():
        return (), None, []
    if not schema_path.is_file() or not impl_path.is_file():
        return (), None, [f"目录型 Skill「{skill_name}」需同时提供 {TOOLS_SCHEMA} 与 {TOOLS_IMPL}"]
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        entries = data.get("tools")
        if not isinstance(entries, list):
            raise ValueError("tools 应为数组")
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry:
                raise ValueError("每个工具需为含 name 的对象")
    except (OSError, ValueError) as exc:
        return (), None, [f"Skill「{skill_name}」的 {TOOLS_SCHEMA} 解析失败：{exc}"]
    return tuple(entries), impl_path, errors


def _materialize_dir_tools(spec: SkillSpec) -> list[Tool]:
    """把目录型 schema 实例化为工具（impl.py 延迟到执行期再加载）。"""
    if not spec.dir_tools or spec.impl_path is None:
        return []
    impl_path = spec.impl_path
    skill_name = spec.name

    def _dispatch(tool_name: str, arguments: dict) -> str:
        spec_ = importlib.util.spec_from_file_location(
            f"eikocode_skill_{skill_name}_impl", impl_path
        )
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)  # type: ignore[union-attr]
        execute = getattr(module, "execute", None)
        if execute is None:
            raise EikoCodeError(
                ErrorKind.TOOL_INVALID_ARG, f"Skill「{skill_name}」的 impl.py 缺少 execute 入口"
            )
        return str(execute(tool_name, arguments))

    tools: list[Tool] = []
    for meta in spec.dir_tools:
        tools.append(
            DirectorySkillTool(
                name=str(meta["name"]),
                description=str(meta.get("description", "")),
                parameters=meta.get("parameters") or {"type": "object", "properties": {}},
                permission=str(meta.get("permission", "write")),
                func=_dispatch,
            )
        )
    return tools


def _scan_dir(base: Path, source_label: str) -> tuple[dict[str, SkillSpec], list[str]]:
    """扫描一个技能目录。返回（名字 → Spec, 错误清单）。"""
    found: dict[str, SkillSpec] = {}
    errors: list[str] = []
    if not base.is_dir():
        return found, errors
    for entry in sorted(base.iterdir()):
        if entry.is_file() and entry.suffix == ".md":
            path, skill_dir = entry, None
        elif entry.is_dir() and (entry / SKILL_ENTRY).is_file():
            path, skill_dir = entry / SKILL_ENTRY, entry
        else:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            spec = parse_skill(text, source=f"{source_label} {path.name}")
            spec.path = path.resolve()  # 热更新与 /skills 详情依赖源文件定位
        except (OSError, SkillParseError) as exc:
            errors.append(f"{path}：{exc}")
            continue
        if skill_dir is not None:
            schemas, impl_path, tool_errors = _load_directory_tools(skill_dir, spec.name)
            if tool_errors:
                errors.extend(tool_errors)
                continue
            spec.dir_tools = schemas
            spec.impl_path = impl_path
        found.setdefault(spec.name, spec)  # 同目录同名：先到先得
    return found, errors


def discover_skills(
    project_dir: Path | None,
    user_dir: Path | None,
    known_tools: set[str],
) -> tuple[list[SkillSpec], list[str]]:
    """三级发现 + 同名覆盖 + fail-fast 白名单校验。

    返回（可用清单[优先级排序], 错误清单）。白名单校验包含目录型 Skill
    自带的工具；fail-fast 的 Skill 不进入清单并点名。
    """
    levels: list[tuple[dict[str, SkillSpec], list[str]]] = []
    if project_dir is not None:
        levels.append(_scan_dir(project_dir, "项目级"))
    if user_dir is not None:
        levels.append(_scan_dir(user_dir, "用户级"))

    merged: dict[str, SkillSpec] = {}
    errors: list[str] = []
    for found, errs in levels:  # 项目级先扫：先到先得即高优先级覆盖
        errors.extend(errs)
        for name, spec in found.items():
            merged.setdefault(name, spec)
    for name, text in BUILTIN_SKILLS:  # 内置最低优先级
        if name not in merged:
            try:
                merged[name] = parse_skill(text, source="内置")
            except SkillParseError as exc:  # 内置样板写错属程序缺陷，直接暴露
                raise EikoCodeError(ErrorKind.CONFIG, f"内置 Skill {name} 解析失败：{exc}") from exc

    # fail-fast：白名单含不存在的工具 → 点名拒绝。
    # 目录型工具在加载期注册进注册表，全部 Skill 的自带工具都视为「存在」，
    # 因此 Skill 可以引用另一个目录型 Skill 的工具（加载顺序无关）。
    all_dir_tools: set[str] = set()
    for spec in merged.values():
        all_dir_tools.update(str(m["name"]) for m in spec.dir_tools)
    available: list[SkillSpec] = []
    for spec in merged.values():
        missing = [t for t in spec.tools if t not in known_tools and t not in all_dir_tools]
        if missing:
            errors.append(
                f"Skill「{spec.name}」（{spec.source}）白名单含不存在的工具：{', '.join(missing)}；已跳过"
            )
            continue
        available.append(spec)
    return available, errors
