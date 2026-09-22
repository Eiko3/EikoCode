"""角色三级加载器（v12）：项目 > 用户级 > 内置，同名覆盖，失败点名跳过。"""

from __future__ import annotations

from pathlib import Path

from ..hooks.yaml_mini import YamlParseError
from .builtin_roles import BUILTIN_ROLES, DEFAULT_ENABLED
from .models import RoleSpec, parse_role

AGENTS_DIRNAME = "agents"


def _scan_dir(base: Path, source_label: str) -> tuple[dict[str, RoleSpec], list[str]]:
    found: dict[str, RoleSpec] = {}
    errors: list[str] = []
    if not base.is_dir():
        return found, errors
    for path in sorted(base.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            spec = parse_role(text, source=f"{source_label} {path.name}")
        except (OSError, YamlParseError) as exc:
            errors.append(f"{path}：{exc}")
            continue
        found.setdefault(spec.name, spec)
    return found, errors


def discover_roles(
    project_dir: Path | None,
    user_dir: Path | None,
    verify_enabled: bool = False,
) -> tuple[list[RoleSpec], list[str]]:
    """三级发现 + 同名覆盖。返回（可用清单[优先级排序], 错误清单）。"""
    merged: dict[str, RoleSpec] = {}
    errors: list[str] = []
    for base, label in ((project_dir, "项目级"), (user_dir, "用户级")):
        if base is None:
            continue
        found, errs = _scan_dir(base, label)
        errors.extend(errs)
        for name, spec in found.items():
            merged.setdefault(name, spec)

    for name, text in BUILTIN_ROLES:  # 内置最低优先级
        if name in merged:
            continue
        if name not in DEFAULT_ENABLED and not (name == "verify" and verify_enabled):
            continue  # verify 由配置开关按需启用
        try:
            merged[name] = parse_role(text, source="内置")
        except YamlParseError as exc:
            errors.append(f"内置角色 {name} 解析失败：{exc}")

    return list(merged.values()), errors
