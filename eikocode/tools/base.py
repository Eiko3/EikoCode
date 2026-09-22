"""工具层：统一工具接口与权限等级。

每个工具实现 `Tool`：声明自身名称、自然语言描述、参数 schema、权限等级，
以及可选的「危险」标记。权限确认与执行器都只认这个接口，不关心工具内部。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PermissionLevel(str, Enum):
    """只读 / 写 / 执行。权限确认策略据此分级。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass(frozen=True)
class ToolSpec:
    """交给供应商声明工具所用的 schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]


class Tool(ABC):
    """统一工具接口。

    具体工具在构造时设置 `name` / `description` / `permission` / `parameters`，
    并实现 `execute`。`execute` 返回字符串（工具结果，会作为消息回写会话）；
    若需以「归一化错误」形式暴露，可抛出 `EikoCodeError`。
    """

    name: str = ""
    description: str = ""
    permission: PermissionLevel = PermissionLevel.READ
    is_dangerous: bool = False
    # 参数的 JSON Schema（供应商 function calling 用）
    parameters: dict[str, Any] = dict(type="object", properties={})

    @abstractmethod
    def execute(self, arguments: dict) -> str:
        """执行工具，返回结果文本。"""
        raise NotImplementedError

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name, description=self.description, input_schema=self.parameters
        )

    def kill(self) -> None:
        """超时时由执行器调用，用于强制终止底层进程。默认无操作。"""
        return None
