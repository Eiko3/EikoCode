"""小组协作系统（v14）：长期小组对象、双后端成员、协作工具、Lead 编排。"""

from .lead import TeamCoordinator
from .mailbox import Mailbox
from .models import MemberSpec, Team
from .tools import COLLAB_TOOL_NAMES

__all__ = [
    "Team", "MemberSpec", "Mailbox", "TeamCoordinator",
    "COLLAB_TOOL_NAMES", "build_team_tools",
]


def build_team_tools(team, member_name: str, mailbox):
    from .tools import build_collab_tools

    return build_collab_tools(team, member_name, mailbox)
