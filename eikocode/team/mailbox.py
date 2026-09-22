"""邮箱两段式消息（v14）：名称注册表 → 实例邮箱文件。

- 注册表落盘 `registry.json`（成员名 / lead → 实例 id）；
- 邮箱 `<小组目录>/mailboxes/<实例id>/inbox.jsonl` 逐行追加；
- 广播 `to="*"` 投递到除发件人外全部成员；协议消息（lifecycle / approval）
  以 `kind` 字段区分。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LEAD_INSTANCE = "lead"


class Mailbox:
    """一个小组的注册表与全部邮箱。"""

    def __init__(self, team_dir: Path) -> None:
        self.dir = Path(team_dir)
        self.registry_file = self.dir / "registry.json"
        self.mailboxes = self.dir / "mailboxes"

    # -- 注册表 --------------------------------------------------------------- #
    def _load_registry(self) -> dict[str, str]:
        if self.registry_file.is_file():
            return json.loads(self.registry_file.read_text(encoding="utf-8"))
        return {}

    def register(self, name: str, instance_id: str) -> None:
        reg = self._load_registry()
        reg[name] = instance_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry_file.write_text(
            json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.mailboxes.mkdir(parents=True, exist_ok=True)

    def resolve(self, name: str) -> str | None:
        return self._load_registry().get(name)

    def members(self) -> list[str]:
        return [n for n in self._load_registry() if n != "lead"]

    # -- 投递 ------------------------------------------------------------------ #
    def send(self, to: str, sender: str, text: str, summary: str = "", kind: str = "message") -> str | None:
        """投递一条消息。返回错误原因（成功返回 None）。

        to = "lead" 投给 Lead；to = "*" 广播给除发件人外全部成员。
        """
        reg = self._load_registry()
        message = {
            "kind": kind,
            "from": sender,
            "text": text,
            "summary": summary or text[:80],
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        targets: list[str] = []
        if to == "*":
            targets = [inst for name, inst in reg.items()
                       if name != sender and name != "lead" or sender != "lead" and name == "lead"]
            # 广播语义：成员广播给其它成员 + Lead；Lead 广播给全部成员
            targets = []
            for name, inst in reg.items():
                if name == sender:
                    continue
                if sender == LEAD_INSTANCE and name == LEAD_INSTANCE:
                    continue
                targets.append(inst)
        elif to == "lead" or to in reg:
            targets = [reg.get(to, LEAD_INSTANCE)]
        else:
            return f"未知收件人：{to}"

        if not targets:
            return f"没有可投递的目标：{to}"
        for inst in targets:
            inbox = self.mailboxes / inst / "inbox.jsonl"
            inbox.parent.mkdir(parents=True, exist_ok=True)
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
        if to != "*" and kind != "message":
            pass  # 协议消息投递后由接收方按 kind 处理
        return None

    def read(self, instance_id: str) -> list[dict]:
        """读取并清空邮箱（消费式）。"""
        inbox = self.mailboxes / instance_id / "inbox.jsonl"
        if not inbox.is_file():
            return []
        messages = []
        for line in inbox.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 坏行跳过
        inbox.write_text("", encoding="utf-8")
        return messages
