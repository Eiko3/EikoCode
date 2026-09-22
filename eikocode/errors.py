"""错误层：把供应商异常、网络异常、解析异常归一为有限类别。

each 类别对应一条固定文案与一个退出码。detail 只用于内部排查，
**任何情况下都不会展示给用户**——它可能包含请求头里带出去的密钥。
"""

from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    MISSING_CREDENTIAL = "missing_credential"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TIMEOUT_IDLE = "timeout_idle"
    NETWORK = "network"
    PROTOCOL = "protocol"
    BILLING = "billing"  # 402 余额不足：用户可直接行动（充值），区别于协议异常
    CONFIG = "config"
    UNKNOWN = "unknown"
    # 工具执行期归一化类别
    TOOL_INVALID_ARG = "tool_invalid_arg"          # 参数非法
    TOOL_TARGET_MISSING = "tool_target_missing"     # 目标不存在
    TOOL_PERMISSION_DENIED = "tool_permission_denied"  # 权限拒绝（OS 层面）
    TOOL_COMMAND_FAILED = "tool_command_failed"     # 命令非零退出
    TOOL_TOO_LARGE = "tool_too_large"               # 文件过大
    TOOL_BINARY = "tool_binary"                     # 二进制文件
    TOOL_ERROR = "tool_error"                        # 工具自定义文案（detail 即用户提示）


# 只有网络类与超时类值得重试；鉴权、限流、协议错误重试多少次结果都一样。
RETRYABLE_KINDS = frozenset({ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.TIMEOUT_IDLE})

_MESSAGES: dict[ErrorKind, str] = {
    ErrorKind.MISSING_CREDENTIAL: (
        "未找到 API Key，请设置环境变量 EIKOCODE_ANTHROPIC_API_KEY 或 EIKOCODE_OPENAI_API_KEY。"
    ),
    ErrorKind.AUTH: "鉴权失败：API Key 无效或已过期，请检查环境变量。",
    ErrorKind.RATE_LIMIT: "已触发限流，重试 2 次后仍失败，请稍后再试。",
    ErrorKind.TIMEOUT: "等待响应超时（30 秒未收到首个字节）。",
    ErrorKind.TIMEOUT_IDLE: "等待响应超时（120 秒未收到新数据）。",
    ErrorKind.NETWORK: "网络连接失败，请检查网络后重试。",
    ErrorKind.PROTOCOL: "响应格式异常，无法解析模型返回的数据。",
    ErrorKind.BILLING: "API 余额不足：请前往供应商控制台充值后重试。",
    ErrorKind.CONFIG: "配置读取失败，请检查配置文件是否为合法 TOML。",
    ErrorKind.UNKNOWN: "发生未知错误。",
    ErrorKind.TOOL_INVALID_ARG: "工具参数不合法：缺少必要的参数或参数类型错误。",
    ErrorKind.TOOL_TARGET_MISSING: "目标不存在：找不到指定的文件或路径。",
    ErrorKind.TOOL_PERMISSION_DENIED: "权限拒绝：无法访问目标，请以更高权限运行或检查路径。",
    ErrorKind.TOOL_COMMAND_FAILED: "命令以非零退出码结束。",
    ErrorKind.TOOL_TOO_LARGE: "文件过大，请改用更精确的搜索。",
    ErrorKind.TOOL_BINARY: "看起来是二进制文件，无法以文本方式读取。",
    ErrorKind.TOOL_ERROR: "工具执行出错。",
}

# 配置类与凭据类问题属于「用户得先去改点什么」，用 2；运行期故障用 1。
_EXIT_CODES: dict[ErrorKind, int] = {
    ErrorKind.MISSING_CREDENTIAL: 2,
    ErrorKind.AUTH: 2,
    ErrorKind.CONFIG: 2,
}

# 这些工具错误的 user_message 直接采用 detail（工具给出的精确话术，如
# 「文件不存在」「命令以非零退出码 2 结束」）。供应商类错误（AUTH/NETWORK…）
# 不在其中，永远走统一静态文案，避免把异常类名泄露给用户。
_DETAIL_KINDS = frozenset(
    {
        ErrorKind.TOOL_ERROR,
        ErrorKind.TOOL_INVALID_ARG,
        ErrorKind.TOOL_TARGET_MISSING,
        ErrorKind.TOOL_PERMISSION_DENIED,
        ErrorKind.TOOL_COMMAND_FAILED,
        ErrorKind.TOOL_TOO_LARGE,
        ErrorKind.TOOL_BINARY,
    }
)

INTERRUPT_MESSAGE = "已中断，本次回复未记入上下文。"


class EikoCodeError(Exception):
    """所有对外暴露的错误都收敛成这一种。"""

    def __init__(self, kind: ErrorKind, detail: str = "") -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.detail = detail

    @property
    def user_message(self) -> str:
        # 工具类错误（见 _DETAIL_KINDS）直接展示工具给出的精确话术，如
        # 「文件不存在」「命令以非零退出码 2 结束」「整次编辑未生效」等；
        # 供应商类错误一律走统一静态文案，不泄露异常类名（详见 checklist.md）。
        if self.kind in _DETAIL_KINDS and self.detail:
            return self.detail
        return _MESSAGES[self.kind]

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES.get(self.kind, 1)


def message_for(kind: ErrorKind) -> str:
    return _MESSAGES[kind]


def exit_code_for(kind: ErrorKind) -> int:
    return _EXIT_CODES.get(kind, 1)


def describe_unexpected(exc: BaseException) -> str:
    """给兜底分支用的描述。

    只暴露异常类型名，不暴露异常内容——第三方库的错误信息里可能
    带着完整的请求头，而请求头里有 API Key。
    """
    return f"发生未知错误：{type(exc).__name__}。请查看 README 的故障排查一节。"
