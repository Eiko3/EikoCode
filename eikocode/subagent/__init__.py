"""子工作者系统（v12）：单一工具入口、角色三级加载、双模式执行、后台任务。

运行时状态隔离（会话 / 权限审批 / 用量），基础设施共享（供应商客户端 /
Hook 引擎 / 文件系统）；三层工具过滤防线杜绝嵌套失控。
"""

from .loader import discover_roles
from .manager import BackgroundTask, SubagentManager, TaskManager
from .models import RoleSpec, parse_role
from .tool import AgentTool

__all__ = [
    "RoleSpec", "parse_role", "AgentTool",
    "TaskManager", "SubagentManager", "BackgroundTask",
    "discover_roles",
]
