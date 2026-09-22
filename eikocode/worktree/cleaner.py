"""过期工作树清理（v13）：启动时执行一次，三层过滤，fail-closed。"""

from __future__ import annotations

import time
from pathlib import Path

from .models import validate_name

# 过期阈值（秒，提议默认 7 天，见 checklist 组 103）
EXPIRE_SECONDS = 7 * 24 * 3600


def cleanup_expired(manager, max_age_seconds: int = EXPIRE_SECONDS, now: float | None = None) -> list[str]:
    """清理过期工作树。返回被删除的名字列表。

    三层过滤：
    1. 命名模式匹配——不满足命名规则的目录不归 EikoCode 管，跳过；
    2. 使用中或未过期——跳过；
    3. 变更与未推送检查——有变更（或检查失败，fail-closed）保留并提示。
    """
    now = now if now is not None else time.time()
    removed: list[str] = []
    base = manager.base
    if not base.is_dir():
        return removed
    for path in sorted(base.iterdir()):
        if not path.is_dir():
            continue
        name = path.relative_to(base).as_posix()
        # 第一层：命名模式（EikoCode 管理的目录才清理）
        if validate_name(name) is not None:
            continue
        # 第二层：使用中 / 未过期
        if manager.current == name:
            continue
        marker = path / ".git"
        mtime = marker.stat().st_mtime if marker.exists() else path.stat().st_mtime
        if now - mtime < max_age_seconds:
            continue
        # 第三层：变更与未推送检查（fail-closed：检查失败按有变更处理）
        if manager.has_changes(path):
            manager.notice(f"清理跳过：工作树 {name} 有未提交 / 未推送变更（fail-closed 保留）")
            continue
        result = manager.delete(name, force=True)
        if "已删除" in result:
            removed.append(name)
            manager.notice(f"已清理过期工作树：{name}")
        else:
            manager.notice(f"清理失败：工作树 {name}（{result}）")
    return removed
