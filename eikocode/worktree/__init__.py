"""Git 工作树隔离（v13）：以 Git 多工作目录机制提供文件系统级任务隔离。

对外入口：`WorktreeManager`（生命周期）、`validate_name`（安全校验）、
`cleanup_expired`（过期清理）。管理入口为 /worktree 斜杠命令与子 Agent
隔离模式，不向模型暴露工具。
"""

from .cleaner import cleanup_expired
from .manager import WorktreeError, WorktreeManager
from .models import branch_for, is_valid_name, validate_name

__all__ = [
    "WorktreeManager",
    "WorktreeError",
    "validate_name",
    "is_valid_name",
    "branch_for",
    "cleanup_expired",
]
