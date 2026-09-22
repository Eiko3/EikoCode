"""适配层（v6）：把远端工具包装成本地统一工具接口。

注册名带服务命名空间前缀（`<服务名>__<工具名>`），避免跨服务重名，
也不再触发注册表的重名报错。权限等级由服务配置决定，未声明按写入类
（保守）。远端返回的非文本内容归一为错误并提示（多模态在范围外）。
"""

from __future__ import annotations

from ..errors import ErrorKind, EikoCodeError
from ..tools.base import PermissionLevel, Tool

_PERMISSION_MAP = {
    "read": PermissionLevel.READ,
    "write": PermissionLevel.WRITE,
    "execute": PermissionLevel.EXECUTE,
}

UNSUPPORTED_CONTENT = "远端返回了不支持的内容类型（当前只处理文本）"


class RemoteTool(Tool):
    """一个远端工具的本地替身。"""

    def __init__(self, server_name: str, spec: dict, client, permission: str = "write"):
        remote_name = str(spec.get("name") or "").strip()
        self._remote_name = remote_name
        self._client = client
        self.name = f"{server_name}__{remote_name}"
        self.description = str(spec.get("description") or f"外部服务 {server_name} 的工具 {remote_name}")
        self.permission = _PERMISSION_MAP.get(permission, PermissionLevel.WRITE)
        schema = spec.get("inputSchema")
        self.parameters = schema if isinstance(schema, dict) else {
            "type": "object",
            "properties": {},
        }

    def execute(self, arguments: dict) -> str:
        result = self._client.call_tool(self._remote_name, dict(arguments or {}))
        if result.get("isError"):
            raise EikoCodeError(ErrorKind.TOOL_ERROR, _content_text(result) or "远端工具返回错误")
        return _content_text(result)

    def kill(self) -> None:
        """超时由执行器调用：终止在途调用（stdio 杀进程树，HTTP 关连接）。"""
        try:
            self._client._transport.kill()  # 仅在超时路径上触达，属执行器语义
        except Exception:
            pass


def _content_text(result: dict) -> str:
    """取结果里的文本内容；非文本内容归一为错误。"""
    content = result.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        else:
            raise EikoCodeError(ErrorKind.TOOL_ERROR, UNSUPPORTED_CONTENT)
    return "\n".join(part for part in parts if part)
