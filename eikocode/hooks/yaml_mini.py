"""迷你 YAML 子集解析器（v11）：为 hooks.yaml 提供「够用就好」的结构解析。

支持：嵌套映射（任意深度）、`- ` 列表（标量项或映射项）、标量
（bool / int / float / 带引号字符串 / 裸字符串）、注释与空行。
不支持：锚点、多行字符串块、流式集合跨行——遇到即按裸字符串处理或
解析失败，由调用方定位报错。不引入 pyyaml 依赖（依赖克制原则）。
"""

from __future__ import annotations


class YamlParseError(ValueError):
    """解析失败（含行号）。"""


def _scalar(raw: str):
    s = raw.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _strip_comment(line: str) -> str:
    # 去掉 # 注释（不在引号内的）
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _split_key(content: str) -> tuple[str, str | None]:
    """把 `key: value` 拆开；无冒号返回 (content, None)。冒号在引号内不算。"""
    quote = None
    for i, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ":" and (i + 1 == len(content) or content[i + 1] in (" ", "\t")):
            value = content[i + 1 :].strip()
            # 无值（`key:`）返回 None，与「值为空串」区分开
            return content[:i].strip(), value if value else None
    return content.strip(), None


def _tokenize(text: str) -> list[tuple[int, int, str]]:
    """行 → (行号, 缩进, 内容)。空行与纯注释剔除。"""
    lines: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.lstrip("\ufeff").replace("\r\n", "\n").splitlines(), start=1):
        stripped = _strip_comment(raw.expandtabs(2))
        content = stripped.lstrip(" ")
        if not content or content == "-":
            continue
        indent = len(stripped) - len(content)
        lines.append((lineno, indent, content))
    return lines


def parse_document(text: str):
    """解析迷你 YAML 文本为 dict / list。结构非法抛 YamlParseError。"""
    lines = _tokenize(text)
    if not lines:
        return {}
    value, pos = _parse_block(lines, 0)
    if pos != len(lines):
        raise YamlParseError(f"第 {lines[pos][0]} 行：意外的缩进或结构")
    return value


def _parse_block(lines: list, pos: int):
    """从 pos 解析一个块（dict 或 list）。返回 (值, 下一位置)。"""
    ind = lines[pos][1]
    if lines[pos][2].startswith("- "):
        return _parse_list(lines, pos, ind)
    return _parse_dict(lines, pos, ind)


def _parse_dict(lines: list, pos: int, indent: int):
    result: dict = {}
    while pos < len(lines):
        lineno, ind, content = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise YamlParseError(f"第 {lineno} 行：意外的缩进")
        if content.startswith("- "):
            break  # 所属列表的下一项
        key, inline = _split_key(content)
        if not key:
            raise YamlParseError(f"第 {lineno} 行：无法解析键")
        if inline is not None:
            result[key] = _scalar(inline)
            pos += 1
        elif pos + 1 < len(lines) and lines[pos + 1][1] > indent:
            result[key], pos = _parse_block(lines, pos + 1)
        else:
            result[key] = None
            pos += 1
    return result, pos


def _parse_list(lines: list, pos: int, indent: int):
    result: list = []
    while pos < len(lines):
        lineno, ind, content = lines[pos]
        if ind != indent or not content.startswith("- "):
            break
        item_content = content[2:].strip()
        key, inline = _split_key(item_content)
        is_mapping = ":" in item_content and key != item_content
        if not is_mapping:
            # 纯标量项
            result.append(_scalar(item_content))
            pos += 1
            continue
        # `key: ...` 映射项——首键内联在列表标记后（虚拟行缩进 +2），
        # 后续属于该项的行（缩进 >= indent+2）并入同一虚拟序列。
        item_indent = indent + 2
        virt: list[tuple[int, int, str]] = [(lineno, item_indent, item_content)]
        j = pos + 1
        while j < len(lines) and lines[j][1] >= item_indent:
            virt.append(lines[j])
            j += 1
        value, _ = _parse_dict(virt, 0, item_indent)
        result.append(value)
        pos = j
    return result, pos
