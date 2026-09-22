"""Skill 系统数据模型（v10）与 frontmatter 迷你解析。

单个 Skill = YAML frontmatter（元信息）+ Markdown 正文（发给模型的 SOP）。
frontmatter 只支持**迷你 YAML 子集**：标量、内联列表 `[a, b]`、多行列表项
（`key:` 换行后若干 `  - item`）。不为此引入完整 YAML 依赖——复杂写法
一律视为解析失败，由调用方点名跳过（spec.md §3 能力 74、75）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 执行模式
MODE_SHARED = "shared"
MODE_ISOLATED = "isolated"
_VALID_MODES = (MODE_SHARED, MODE_ISOLATED)

# 隔离模式上下文携带三档（提议默认：none；见 checklist 组 78）
CONTEXT_FULL = "full"
CONTEXT_RECENT = "recent"
CONTEXT_NONE = "none"
_VALID_CONTEXTS = (CONTEXT_FULL, CONTEXT_RECENT, CONTEXT_NONE)

# Skill 名字约束：小写字母数字连字符下划线（同时用作 /短命令 名）
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# frontmatter 必填 / 合法键
_REQUIRED_KEYS = ("name",)
_KNOWN_KEYS = ("name", "description", "tools", "mode", "model", "context")


class SkillParseError(ValueError):
    """frontmatter 解析失败（含原因），由加载器点名文件后跳过。"""


@dataclass
class SkillSpec:
    """一个已解析的 Skill。"""

    name: str
    description: str
    sop: str  # 正文原样，不改写
    mode: str = MODE_SHARED
    tools: tuple[str, ...] = ()  # 白名单；空 = 不收窄
    model: str = ""  # 空 = 用当前会话模型
    context: str = CONTEXT_NONE  # 隔离模式携带档位
    source: str = "内置"  # 来源描述（清单 / 报错用）
    path: Path | None = None  # 源文件路径（None = 内置，不支持热更新）
    dir_tools: tuple[dict, ...] = field(default_factory=tuple)  # 目录型工具 schema
    impl_path: Path | None = None  # 目录型 impl.py

    @property
    def is_isolated(self) -> bool:
        return self.mode == MODE_ISOLATED


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_inline_list(value: str) -> list[str]:
    inner = value.strip()[1:-1]
    if not inner.strip():
        return []
    return [_strip_quotes(item) for item in inner.split(",") if item.strip()]


def _parse_mini_yaml(text: str) -> dict:
    """迷你 YAML 子集解析。不支持的结构抛 SkillParseError。"""
    data: dict = {}
    current_list_key: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if line.startswith("- "):
            # 多行列表项：必须紧跟在 `key:` 之后
            if current_list_key is None or indent == 0:
                raise SkillParseError(f"第 {lineno} 行：列表项缺少所属键或顶格")
            data[current_list_key].append(_strip_quotes(line[2:]))
            continue
        current_list_key = None
        if ":" not in line:
            raise SkillParseError(f"第 {lineno} 行：不是「键: 值」结构")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if indent != 0:
            raise SkillParseError(f"第 {lineno} 行：不支持嵌套结构")
        if not value:
            # 空值：可能是多行列表的开头，先占位
            data[key] = []
            current_list_key = key
            continue
        if value.startswith("[") and value.endswith("]"):
            data[key] = _parse_inline_list(value)
        else:
            data[key] = _strip_quotes(value)
    return data


def parse_skill(text: str, source: str) -> SkillSpec:
    """把一段 Markdown（frontmatter + 正文）解析为 SkillSpec。

    解析失败抛 SkillParseError（含 source 与原因）；正文原样保留。
    Windows 工具链写的 UTF-8 文件常带 BOM，这里剥掉再解析。
    """
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---"):
        raise SkillParseError("缺少 frontmatter（应以 --- 开头）")
    end = text.find("\n---", 3)
    if end < 0:
        raise SkillParseError("frontmatter 未闭合（缺第二个 ---）")
    header = text[3:end]
    # 跳过闭合 --- 行及其换行
    body_start = text.index("\n", end + 1) + 1 if "\n" in text[end + 1 :] else len(text)
    sop = text[body_start:].strip("\n")
    if not sop.strip():
        raise SkillParseError("正文（SOP）为空")

    data = _parse_mini_yaml(header)
    for key in data:
        if key not in _KNOWN_KEYS:
            raise SkillParseError(f"未知 frontmatter 键：{key}")
    for key in _REQUIRED_KEYS:
        if key not in data or not str(data[key]).strip():
            raise SkillParseError(f"缺少必填键：{key}")

    name = str(data["name"]).strip()
    if not _NAME_RE.match(name):
        raise SkillParseError(f"name 非法（需小写字母开头，仅小写字母数字-_）：{name}")

    mode = str(data.get("mode") or MODE_SHARED).strip()
    if mode not in _VALID_MODES:
        raise SkillParseError(f"mode 非法（应为 shared / isolated）：{mode}")

    context = str(data.get("context") or CONTEXT_NONE).strip()
    if context not in _VALID_CONTEXTS:
        raise SkillParseError(f"context 非法（应为 full / recent / none）：{context}")

    tools_raw = data.get("tools") or []
    if isinstance(tools_raw, str):
        tools_raw = [tools_raw]
    if not isinstance(tools_raw, list):
        raise SkillParseError("tools 应为列表")

    description = str(data.get("description") or "").strip() or name
    return SkillSpec(
        name=name,
        description=description,
        sop=sop,
        mode=mode,
        tools=tuple(str(t).strip() for t in tools_raw if str(t).strip()),
        model=str(data.get("model") or "").strip(),
        context=context,
        source=source,
    )
