"""配置层：环境变量 > 项目级配置 > 用户级配置。

两条硬约束：
1. 凭据只从环境变量读取，本模块不提供任何写入磁盘的代码路径。
2. 配置文件里只放非敏感项（模型名、采样参数、上下文上限）。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ErrorKind, EikoCodeError

ENV_ANTHROPIC_KEY = "EIKOCODE_ANTHROPIC_API_KEY"
ENV_OPENAI_KEY = "EIKOCODE_OPENAI_API_KEY"
ENV_OPENAI_BASE_URL = "EIKOCODE_OPENAI_BASE_URL"
ENV_MODEL = "EIKOCODE_MODEL"
ENV_AUTO_APPROVE = "EIKOCODE_AUTO_APPROVE"
ENV_PERMISSION_MODE = "EIKOCODE_PERMISSION_MODE"

PROJECT_CONFIG_NAME = ".eikocode.toml"
USER_CONFIG_PATH = Path.home() / ".eikocode" / "config.toml"

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 4096
DEFAULT_CONTEXT_LIMIT = 200000
DEFAULT_AUTO_APPROVE = False

# 布尔配置的假值集合（不分大小写）。空串按「未设置」处理，落到 default。
_FALSEY = frozenset({"", "0", "off", "false", "no", "none"})

# /model 命令用这张前缀表做离线校验。用户可在配置文件里用 known_models 扩展。
KNOWN_MODEL_PREFIXES = (
    "claude",
    "gpt-",
    "o1",
    "o3",
    "o4",
    "chatgpt",
    "deepseek",
    "qwen",
    "glm",
    "kimi",
    "moonshot",
    "llama",
    "mistral",
    "mixtral",
    "gemini",
    "ernie",
    "doubao",
    "yi-",
    "internlm",
    "baichuan",
    "phi-",
    "gemma",
    "command-r",
    "sonar",
)


@dataclass(frozen=True)
class Config:
    model: str
    temperature: float
    max_tokens: int
    context_limit: int
    known_models: tuple[str, ...]
    anthropic_api_key: str | None
    openai_api_key: str | None
    openai_base_url: str | None
    # 自动批准：true 时读 / 写 / 非危险 Shell 不再逐条确认（危险命令仍拦）。
    auto_approve: bool = DEFAULT_AUTO_APPROVE
    # 权限档位（v5）：strict / default / permissive。空串 = 未设置，
    # 由消费方（安全流水）按 auto_approve 映射——直接构造 Config 的路径
    # （如测试）也要能走通映射。
    permission_mode: str = ""
    # 沙箱追加目录（v5）：在默认工作目录之外允许读写的目录。
    sandbox_dirs: tuple[str, ...] = ()
    # 安全规则（v5）：[[rules]] 数组；用户级与项目级分开携带以保留来源标记。
    user_rules: tuple[dict, ...] = ()
    project_rules: tuple[dict, ...] = ()
    # v12：verify 角色（子工作者）配置开关，缺省关闭
    verify_role: bool = False
    # v14：纯调度模式配置锁（与会话内显式命令双重锁定，缺省关）
    team_dispatch_only: bool = False
    # 外部工具服务（v6）：已按同名覆盖合并后的服务列表。
    mcp_servers: tuple[McpServerConfig, ...] = ()
    # 外部工具服务的配置错误（启动时明确报出，该服务不进工具集）。
    mcp_errors: tuple[str, ...] = ()
    # 上下文压缩（v7）：自动兜底开关与阈值（提议默认见 checklist 组 55）。
    auto_compact: bool = True
    summary_model: str = ""
    compact_tool_result_chars: int = 20_000
    compact_message_chars: int = 40_000
    compact_preview_chars: int = 1_500

    @property
    def has_any_credential(self) -> bool:
        return bool(self.anthropic_api_key or self.openai_api_key)


def _read_toml(path: Path) -> dict:
    """读一个配置文件。文件不存在算正常，返回空配置。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EikoCodeError(ErrorKind.CONFIG, f"{path}: {type(exc).__name__}") from exc
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise EikoCodeError(ErrorKind.CONFIG, f"{path}: {type(exc).__name__}") from exc


def _as_float(data: dict, key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError) as exc:
        raise EikoCodeError(ErrorKind.CONFIG, f"{key}: {type(exc).__name__}") from exc


def _as_int(data: dict, key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError) as exc:
        raise EikoCodeError(ErrorKind.CONFIG, f"{key}: {type(exc).__name__}") from exc


def _as_bool(value: object, default: bool) -> bool:
    """读布尔配置。环境变量 / TOML 里的 0、off、false、no、none 判假（不分大小写）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSEY


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """读字符串数组配置（如沙箱追加目录）；非法条目跳过。"""
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _as_dict_tuple(value: object) -> tuple[dict, ...]:
    """读表数组配置（如安全规则）；非表条目跳过，合法性由使用方校验。"""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# --------------------------------------------------------------------------- #
# 外部工具服务（v6）
# --------------------------------------------------------------------------- #
PERMISSION_LEVELS = ("read", "write", "execute")

# 环境变量引用语法：${VAR}（配置本身不承载密钥）
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

MCP_DEFAULT_TIMEOUT = 120.0
MCP_DEFAULT_PERMISSION = "write"


def _expand_env_refs(value: str) -> str:
    """把 ${VAR} 展开为宿主环境变量；未定义时明确报错，不静默填空。"""

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is None:
            raise EikoCodeError(ErrorKind.CONFIG, f"环境变量未定义：{name}")
        return found

    return _ENV_REF.sub(_sub, value)


@dataclass(frozen=True)
class McpServerConfig:
    """一个外部工具服务（v6）。stdio 用 argv，HTTP 用 url，二者互斥。"""

    name: str
    argv: tuple[str, ...] = ()
    url: str = ""
    env: tuple[tuple[str, str], ...] = ()
    timeout: float = MCP_DEFAULT_TIMEOUT
    permission: str = MCP_DEFAULT_PERMISSION


def parse_mcp_servers(items: object) -> tuple[McpServerConfig, ...]:
    """解析 [[mcp_servers]]。配置错误一律明确报出（不静默跳过）。"""
    servers: list[McpServerConfig] = []
    seen: set[str] = set()
    for item in items if isinstance(items, (list, tuple)) else ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            raise EikoCodeError(ErrorKind.CONFIG, "外部工具服务缺少 name")
        if name in seen:
            raise EikoCodeError(ErrorKind.CONFIG, f"外部工具服务名重复：{name}")
        seen.add(name)

        raw_command = item.get("command")
        raw_args = item.get("args") or ()
        url = str(item.get("url") or "").strip()
        if bool(raw_command) == bool(url):
            raise EikoCodeError(
                ErrorKind.CONFIG,
                f"外部工具服务 {name} 必须且只能指定 command 或 url 之一",
            )

        argv: tuple[str, ...] = ()
        if raw_command:
            head = (
                (str(raw_command),)
                if isinstance(raw_command, str)
                else tuple(str(part) for part in raw_command)
            )
            tail = (
                (str(raw_args),)
                if isinstance(raw_args, str)
                else tuple(str(part) for part in raw_args)
            )
            argv = head + tail

        env: dict[str, str] = {}
        raw_env = item.get("env") or {}
        if isinstance(raw_env, dict):
            for key, value in raw_env.items():
                env[str(key)] = _expand_env_refs(str(value))

        permission = str(item.get("permission") or MCP_DEFAULT_PERMISSION).strip().lower()
        if permission not in PERMISSION_LEVELS:
            raise EikoCodeError(
                ErrorKind.CONFIG,
                f"外部工具服务 {name} 的 permission 非法：{permission}",
            )

        servers.append(
            McpServerConfig(
                name=name,
                argv=argv,
                url=url,
                env=tuple(sorted(env.items())),
                timeout=_as_float(item, "timeout", MCP_DEFAULT_TIMEOUT),
                permission=permission,
            )
        )
    return tuple(servers)


def _safe_parse_mcp(raw: object, errors: list[str]) -> tuple[McpServerConfig, ...]:
    """解析服务列表；配置错误收集进 errors（启动时明确报出），不崩掉整个配置。"""
    try:
        return parse_mcp_servers(raw)
    except EikoCodeError as exc:
        errors.append(exc.detail or exc.user_message)
        return ()


def _merge_mcp_servers(
    user_data: dict, project_data: dict, errors: list[str]
) -> tuple[McpServerConfig, ...]:
    """合并两级配置：项目级覆盖用户级（同名服务整条覆盖）。"""
    merged: dict[str, McpServerConfig] = {}
    for server in _safe_parse_mcp(user_data.get("mcp_servers"), errors):
        merged[server.name] = server
    for server in _safe_parse_mcp(project_data.get("mcp_servers"), errors):
        merged[server.name] = server
    return tuple(merged.values())


def load_config(cwd: Path | None = None) -> Config:
    base = cwd if cwd is not None else Path.cwd()

    # 优先级从低到高依次覆盖：内置默认值 → 用户级 → 项目级 → 环境变量。
    # 规则不合并：user_data / project_data 分开保留，来源标记供安全流水展示。
    user_data: dict = _read_toml(USER_CONFIG_PATH)
    project_data: dict = _read_toml(base / PROJECT_CONFIG_NAME)
    data: dict = {**user_data, **project_data}

    known = data.get("known_models", ())
    if isinstance(known, str):
        known = (known,)

    auto_approve = _as_bool(
        os.environ.get(ENV_AUTO_APPROVE) or None,
        _as_bool(data.get("auto_approve"), DEFAULT_AUTO_APPROVE),
    )
    # 外部工具服务的配置错误收集后报出（该服务不进工具集，其余照常），
    # 而不是让整个配置加载失败。
    mcp_errors: list[str] = []
    # 档位：显式配置（环境变量 > TOML）优先；未显式配置时由 v4 自动批准映射
    # （true → 放行档），保证 v4 行为在 v5 档位体系下不变。
    permission_mode = (
        (os.environ.get(ENV_PERMISSION_MODE) or str(data.get("permission_mode") or ""))
        .strip()
        .lower()
        or ("permissive" if auto_approve else "default")
    )

    return Config(
        model=os.environ.get(ENV_MODEL) or str(data.get("model") or DEFAULT_MODEL),
        temperature=_as_float(data, "temperature", DEFAULT_TEMPERATURE),
        max_tokens=_as_int(data, "max_tokens", DEFAULT_MAX_TOKENS),
        context_limit=_as_int(data, "context_limit", DEFAULT_CONTEXT_LIMIT),
        known_models=tuple(str(item) for item in known),
        anthropic_api_key=os.environ.get(ENV_ANTHROPIC_KEY) or None,
        openai_api_key=os.environ.get(ENV_OPENAI_KEY) or None,
        openai_base_url=os.environ.get(ENV_OPENAI_BASE_URL) or data.get("openai_base_url") or None,
        # `or None`：空串按「未设置」处理，否则会覆盖掉配置文件里显式开的 true。
        auto_approve=auto_approve,
        permission_mode=permission_mode,
        sandbox_dirs=_as_str_tuple(data.get("sandbox_dirs")),
        user_rules=_as_dict_tuple(user_data.get("rules")),
        project_rules=_as_dict_tuple(project_data.get("rules")),
        verify_role=_as_bool(data.get("verify_role"), False),
        team_dispatch_only=_as_bool(data.get("team_dispatch_only"), False),
                mcp_servers=_merge_mcp_servers(user_data, project_data, mcp_errors),
        mcp_errors=tuple(mcp_errors),
        auto_compact=_as_bool(data.get("auto_compact"), True),
        summary_model=str(data.get("summary_model") or "").strip(),
        compact_tool_result_chars=max(
            1, _as_int(data, "compact_tool_result_chars", 20_000)
        ),
        compact_message_chars=max(
            1, _as_int(data, "compact_message_chars", 40_000)
        ),
        compact_preview_chars=max(
            1, _as_int(data, "compact_preview_chars", 1_500)
        ),
    )


def is_known_model(name: str, extra: tuple[str, ...] = ()) -> bool:
    """离线校验模型名。拒绝空串、含空白的串，以及不在前缀表里的名字。"""
    lowered = name.strip().lower()
    if not lowered:
        return False
    if any(char.isspace() for char in lowered):
        return False
    for prefix in (*extra, *KNOWN_MODEL_PREFIXES):
        if lowered.startswith(prefix.lower()):
            return True
    return False
