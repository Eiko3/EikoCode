"""项目指令与会话记忆（v8）。

三个子模块：
- instructions：项目/用户两级指令文件（EIKOCODE.md）的发现、@include 展开
- archive：会话存档（JSONL 逐行追加 + 元数据）与恢复
- notes：自动笔记（四类内容、两级目录、异步更新）
"""

from .instructions import (
    INSTRUCTION_FILENAME,
    load_instructions,
)
from .archive import SessionArchiver, list_archives, load_archive, new_session_id
from .notes import NotesManager

__all__ = [
    "INSTRUCTION_FILENAME",
    "NotesManager",
    "SessionArchiver",
    "list_archives",
    "load_instructions",
    "load_archive",
    "new_session_id",
]
