"""会话存档（v8）：JSONL 逐行追加 + 元数据概要。

存档位置（提议默认）：`.eikocode/sessions/<会话id>.jsonl` 与同名
`.meta.json`。追加写开销恒定，崩溃最多丢最后一行不完整数据；恢复时
坏行跳过、未配对的工具调用截断到最后完整位置（spec.md §3 能力 62、63）。
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..errors import ErrorKind, EikoCodeError
from ..providers.base import Message, Role, ToolCall

SESSIONS_DIRNAME = ".eikocode/sessions"

# 时间跨度提醒阈值（提议默认 1 天，见 checklist 组 61）
STALE_REMIND_DELTA = timedelta(days=1)


def new_session_id() -> str:
    """会话 id：时间戳 + 短随机串，保证可排序且不冲突。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _message_to_row(m: Message) -> dict:
    row: dict = {"role": m.role.value, "content": m.content}
    if m.tool_calls:
        row["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        row["tool_call_id"] = m.tool_call_id
    return row


def _row_to_message(row: dict) -> Message | None:
    role = row.get("role")
    if role not in ("user", "assistant"):
        return None
    tool_calls = tuple(
        ToolCall(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", {}))
        for tc in row.get("tool_calls") or ()
        if isinstance(tc, dict)
    )
    return Message(
        role=Role(role),
        content=str(row.get("content", "")),
        tool_calls=tool_calls,
        tool_call_id=row.get("tool_call_id"),
    )


class SessionArchiver:
    """一个活跃会话的归档器：append 模式打开，随写随更 meta。"""

    def __init__(
        self,
        base_dir: Path | None = None,
        session_id: str | None = None,
        title: str = "",
        message_count: int = 0,
        created_at: str | None = None,
    ):
        self.session_id = session_id or new_session_id()
        base = Path(base_dir) if base_dir else Path.cwd()
        self.directory = base / SESSIONS_DIRNAME
        self.path = self.directory / f"{self.session_id}.jsonl"
        self.meta_path = self.directory / f"{self.session_id}.meta.json"
        self._lock = threading.Lock()
        self._count = int(message_count)
        self._title = title
        self._created_at = created_at or datetime.now().isoformat(timespec="seconds")
        self.directory.mkdir(parents=True, exist_ok=True)

    def append(self, message: Message) -> None:
        """追加一条消息并更新元数据。写失败静默（存档不阻断对话）。"""
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_message_to_row(message), ensure_ascii=False) + "\n")
                self._count += 1
                if message.role is Role.USER and message.tool_call_id is None and not self._title:
                    self._title = message.content[:30]
                self._write_meta()
            except (OSError, TypeError):
                pass  # 存档是尽力而为的旁路，不阻断对话

    def _write_meta(self) -> None:
        meta = {
            "id": self.session_id,
            "title": self._title or "（空会话）",
            "message_count": self._count,
            "created_at": self._created_at,
            "last_active_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def close(self) -> None:
        self._write_meta()
        # 空会话（一次对话都没发生）没有保留价值，撤掉两个文件
        if self._count == 0:
            try:
                self.path.unlink(missing_ok=True)
                self.meta_path.unlink(missing_ok=True)
            except OSError:
                pass


def _archive_dir(base_dir: Path | None) -> Path:
    base = Path(base_dir) if base_dir else Path.cwd()
    return base / SESSIONS_DIRNAME


def list_archives(base_dir: Path | None = None) -> list[dict]:
    """列出全部会话元数据，按最近活跃倒序。"""
    directory = _archive_dir(base_dir)
    metas: list[dict] = []
    if not directory.is_dir():
        return metas
    for meta_path in directory.glob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("id"):
                metas.append(meta)
        except (OSError, json.JSONDecodeError):
            continue
    metas.sort(key=lambda m: m.get("last_active_at", ""), reverse=True)
    return metas


def load_archive(session_id: str, base_dir: Path | None = None) -> tuple[list[Message], dict]:
    """按 id 读存档，返回（修复后的消息列表, 元数据）。

    异常处理（spec.md §3 能力 63）：解析失败的行跳过；未配对的工具调用
    截断到最后一个完整位置。存档不存在抛错。
    """
    path = _archive_dir(base_dir) / f"{session_id}.jsonl"
    meta_path = _archive_dir(base_dir) / f"{session_id}.meta.json"
    if not path.is_file():
        raise EikoCodeError(ErrorKind.CONFIG, f"会话存档不存在：{session_id}")

    messages: list[Message] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏行跳过
        if not isinstance(row, dict):
            continue
        message = _row_to_message(row)
        if message is not None:
            messages.append(message)

    messages = _truncate_unpaired(messages)

    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
    return messages, meta


def _truncate_unpaired(messages: list[Message]) -> list[Message]:
    """未配对的工具调用截断到最后一个完整位置。

    扫描：某条带工具调用的助手消息，其后（直到下一条助手消息之前）没有
    对应 tool_call_id 的结果消息 → 该助手消息起全部截断。
    """
    for i, m in enumerate(messages):
        if m.role is Role.ASSISTANT and m.tool_calls:
            expected = {tc.id for tc in m.tool_calls}
            answered = {
                later.tool_call_id
                for later in messages[i + 1 :]
                if later.tool_call_id is not None
            }
            if not expected <= answered:
                return messages[:i]
    return messages


def stale_days(meta: dict, now: datetime | None = None) -> int | None:
    """距上次活跃的天数；meta 无时间信息时返回 None。"""
    raw = meta.get("last_active_at")
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    reference = now or datetime.now()
    return max(0, (reference - last).days)
