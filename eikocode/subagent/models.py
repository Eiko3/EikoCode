"""子工作者角色模型与解析（v12）。

角色 = YAML frontmatter（元信息）+ Markdown 正文（角色 SOP）。
frontmatter 复用 v11 的 yaml_mini 解析；三级加载与失败点名沿用
v10 Skill 的模式（spec.md §3 能力 93）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..hooks.yaml_mini import YamlParseError

# 角色名约束（同时作为清单位置标识）
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

_PERMISSION_MODES = ("strict", "default", "permissive")

# 后台运行时的只读工具集合（提议默认，见 checklist 组 90）
READONLY_TOOLS = frozenset({"ReadFile", "Glob", "Grep"})

# 角色缺省最大轮次（提议默认，见 checklist 组 91）
DEFAULT_MAX_TURNS = 20

_KNOWN_KEYS = (
    "name", "description", "tools", "deny_tools",
    "model", "max_turns", "permission_mode", "worktree",
)


@dataclass
class RoleSpec:
    """一个已解析的子工作者角色。"""

    name: str
    description: str
    sop: str  # 角色指令，原样保留
    tools: tuple[str, ...] = ()  # 白名单；空 = 不收窄
    deny_tools: tuple[str, ...] = ()  # 黑名单
    model: str = ""  # 空 = 用当前会话模型
    max_turns: int = DEFAULT_MAX_TURNS
    permission_mode: str = ""  # 空 = 继承当前档位
    worktree: bool = False  # v13：工作树隔离模式
    source: str = "内置"


def split_frontmatter(text: str) -> tuple[str, str]:
    """拆出 frontmatter 与正文。缺失 / 未闭合抛 YamlParseError。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---"):
        raise YamlParseError("缺少 frontmatter（应以 --- 开头）")
    end = text.find("\n---", 3)
    if end < 0:
        raise YamlParseError("frontmatter 未闭合（缺第二个 ---）")
    header = text[3:end]
    body_start = text.index("\n", end + 1) + 1 if "\n" in text[end + 1 :] else len(text)
    return header, text[body_start:].strip("\n")


def _parse_flat_mapping(text: str) -> dict:
    """扁平键值解析：标量 + 内联列表 + 多行列表项（同 v10 Skill 子集）。"""
    data: dict = {}
    current_list_key: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("- "):
            if current_list_key is None or indent == 0:
                raise YamlParseError(f"第 {lineno} 行：列表项缺少所属键或顶格")
            data[current_list_key].append(_scalar(line[2:]))
            continue
        current_list_key = None
        if ":" not in line:
            raise YamlParseError(f"第 {lineno} 行：不是「键: 值」结构")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if indent != 0:
            raise YamlParseError(f"第 {lineno} 行：不支持嵌套结构")
        if not value:
            data[key] = []
            current_list_key = key
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            data[key] = [_scalar(x) for x in inner.split(",") if x.strip()] if inner.strip() else []
        else:
            data[key] = _scalar(value)
    return data


def _scalar(raw: str):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def parse_role(text: str, source: str) -> RoleSpec:
    """把角色 Markdown 解析为 RoleSpec。失败抛 YamlParseError（含原因）。"""
    header, sop = split_frontmatter(text)
    if not sop.strip():
        raise YamlParseError("正文（角色 SOP）为空")
    try:
        data = _parse_flat_mapping(header)
    except YamlParseError as exc:
        raise YamlParseError(f"frontmatter 第 {exc} 处：{exc}") from exc

    for key in data:
        if key not in _KNOWN_KEYS:
            raise YamlParseError(f"未知 frontmatter 键：{key}")

    name = str(data.get("name", "")).strip()
    if not _NAME_RE.match(name):
        raise YamlParseError(f"name 非法（需小写字母开头，仅小写字母数字-_）：{name or '（缺失）'}")

    mode = str(data.get("permission_mode", "")).strip().lower()
    if mode and mode not in _PERMISSION_MODES:
        raise YamlParseError(f"permission_mode 非法（strict / default / permissive）：{mode}")

    try:
        max_turns = int(data.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        raise YamlParseError(f"max_turns 应为整数：{data.get('max_turns')}")

    def _str_list(key: str) -> tuple[str, ...]:
        raw = data.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise YamlParseError(f"{key} 应为列表")
        return tuple(str(x).strip() for x in raw if str(x).strip())

    description = str(data.get("description") or "").strip() or name
    return RoleSpec(
        name=name,
        description=description,
        sop=sop,
        tools=_str_list("tools"),
        deny_tools=_str_list("deny_tools"),
        model=str(data.get("model") or "").strip(),
        max_turns=max_turns,
        permission_mode=mode,
        worktree=bool(data.get("worktree", False)),
        source=source,
    )
