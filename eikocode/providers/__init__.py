"""供应商选择与装配。

这里只在函数内部 import 具体适配模块——这样 `from .providers.base import Message`
不会顺带把两家 SDK 拖进进程，会话层和用量层可以保持干净。
"""

from __future__ import annotations

from ..config import Config
from ..errors import ErrorKind, EikoCodeError
from .base import GenerateParams, Message, Provider, Role

ANTHROPIC_MODEL_PREFIX = "claude"

__all__ = [
    "ANTHROPIC_MODEL_PREFIX",
    "GenerateParams",
    "Message",
    "Provider",
    "Role",
    "select_provider",
]


def select_provider(config: Config, model: str | None = None) -> Provider:
    """按模型名前缀挑供应商。缺少对应凭据时抛凭据错误。"""
    from .anthropic import AnthropicProvider
    from .openai_compat import OpenAICompatProvider

    name = (model or config.model).strip().lower()
    if name.startswith(ANTHROPIC_MODEL_PREFIX):
        if not config.anthropic_api_key:
            raise EikoCodeError(ErrorKind.MISSING_CREDENTIAL, "anthropic")
        return AnthropicProvider(config.anthropic_api_key)
    if not config.openai_api_key:
        raise EikoCodeError(ErrorKind.MISSING_CREDENTIAL, "openai")
    return OpenAICompatProvider(config.openai_api_key, config.openai_base_url)
