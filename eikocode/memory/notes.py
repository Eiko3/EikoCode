"""自动笔记（v8）：四类内容、两级目录、异步更新。

分类与归属（提议默认，见 checklist 组 62）：
- 用户级 `~/.eikocode/notes.md`：用户偏好、纠正反馈
- 项目级 `<项目>/.eikocode/notes.md`：项目知识、参考资料

触发：每 N 轮（提议默认 5）对话结束 + 会话退出时。轮次触发走后台线程
（不阻塞对话）；退出触发同步执行（保证写完）。更新 = 模型读当前笔记与
最近对话 → 输出更新后的笔记全文 → 写回；失败静默跳过，下轮再试。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Sequence

from ..providers.base import GenerateParams, Message, Role

USER_NOTES_FILENAME = "notes.md"
PROJECT_NOTES_DIRNAME = ".eikocode"

# 笔记更新触发间隔（提议默认 5 轮，见 checklist 组 62）
NOTES_INTERVAL_TURNS = 5

USER_CATEGORIES = ("用户偏好", "纠正反馈")
PROJECT_CATEGORIES = ("项目知识", "参考资料")


def _empty_note(categories: Sequence[str]) -> str:
    return "\n".join(f"## {name}\n（暂无）" for name in categories) + "\n"


def _serialize_recent(messages: Sequence[Message], limit: int = 20) -> str:
    lines: list[str] = []
    for m in messages[-limit:]:
        if m.role is Role.USER and m.tool_call_id is not None:
            continue
        label = "用户" if m.role is Role.USER else "助手"
        lines.append(f"[{label}] {m.content[:500]}")
    return "\n".join(lines)


class NotesManager:
    """轮次计数、异步触发与笔记写回。"""

    def __init__(
        self,
        config,
        notify: Callable[[str], None],
        project_dir: Path | None = None,
        user_dir: Path | None = None,
        interval: int = NOTES_INTERVAL_TURNS,
    ):
        self._config = config
        self._notify = notify
        self._interval = max(1, int(interval))
        self._turn_count = 0
        self._lock = threading.Lock()
        base_user = user_dir if user_dir is not None else Path.home() / ".eikocode"
        base_project = (
            project_dir if project_dir is not None else Path.cwd() / PROJECT_NOTES_DIRNAME
        )
        self.user_notes_path = base_user / USER_NOTES_FILENAME
        self.project_notes_path = base_project / USER_NOTES_FILENAME
        self._base_project = base_project

    def rebase(self, project_dir) -> None:
        """工作目录切换后重载项目级笔记路径（v13 缓存清理）。"""
        self._base_project = Path(project_dir) / PROJECT_NOTES_DIRNAME
        self.project_notes_path = self._base_project / USER_NOTES_FILENAME

    # -- 触发 ---------------------------------------------------------------- #
    def on_turn_end(self, session) -> None:
        """对话轮结束时调用：计数达标则异步触发一次更新。"""
        with self._lock:
            self._turn_count += 1
            due = self._turn_count >= self._interval
            if due:
                self._turn_count = 0
        if due:
            self._spawn(session)

    def update_now(self, session) -> None:
        """同步更新（退出时调用，保证写完）。

        会话里没有任何成功的助手回复时跳过——空会话 / 被拦截的会话
        没有值得记录的内容，不值得一次模型调用。
        """
        if not any(m.role is Role.ASSISTANT for m in session.messages()):
            return
        self._update(session)

    def _spawn(self, session) -> None:
        thread = threading.Thread(target=self._update, args=(session,), daemon=True)
        thread.start()

    # -- 更新 ---------------------------------------------------------------- #
    def _update(self, session) -> None:
        try:
            from ..providers import select_provider

            model = str(getattr(self._config, "summary_model", "") or "") or self.config_model()
            provider = select_provider(self._config, model)
            self._update_one(provider, model, self.user_notes_path, USER_CATEGORIES, session)
            self._update_one(provider, model, self.project_notes_path, PROJECT_CATEGORIES, session)
        except Exception:
            pass  # 笔记是旁路：任何失败都静默跳过，下轮再试

    def config_model(self) -> str:
        return str(getattr(self._config, "model", "") or "")

    def _update_one(self, provider, model: str, path: Path, categories: Sequence[str], session) -> None:
        try:
            try:
                current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            except OSError:
                current = ""
            categories_text = "、".join(categories)
            prompt = (
                "【任务】更新长期笔记。只输出更新后的完整笔记内容（Markdown），不要解释，"
                "不要输出任何标签以外的内容。\n"
                f"【分类】笔记只允许以下 {len(categories)} 个小节，用「## 标题」开头：{categories_text}。\n"
                "【规则】合并与去重由你判断：新信息并入对应小节，过时的信息可改写，"
                "与对话无关的内容不要添加；没有新信息时原样返回。\n"
                f"【当前笔记】\n{current or '（空）'}\n"
                f"【最近对话】\n{_serialize_recent(session.messages())}"
            )
            chunks: list[str] = []
            for item in provider.stream(
                [Message(role=Role.USER, content=prompt)],
                GenerateParams(
                    model=model,
                    temperature=0.2,
                    max_tokens=4096,
                    system="你是 EikoCode 的笔记管家。只输出更新后的笔记全文，使用简体中文。",
                ),
            ):
                if isinstance(item, str):
                    chunks.append(item)
            text = "".join(chunks).strip()
            if not text:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        except Exception:
            return  # 笔记是旁路：模型或写盘失败都静默跳过，原笔记不被破坏
