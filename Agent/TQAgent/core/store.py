"""会话持久化：保存 / 列出 / 恢复历史会话。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


SESSIONS_DIRNAME = "sessions"


@dataclass
class SessionRecord:
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    rounds: int = 0
    tool_calls: int = 0
    model: str = ""
    agents_md_injected: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": self.messages,
            "events": self.events,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "model": self.model,
            "agents_md_injected": self.agents_md_injected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRecord":
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or "未命名会话"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            messages=list(data.get("messages") or []),
            events=list(data.get("events") or []),
            rounds=int(data.get("rounds") or 0),
            tool_calls=int(data.get("tool_calls") or 0),
            model=str(data.get("model") or ""),
            agents_md_injected=bool(data.get("agents_md_injected")),
        )


def sessions_dir(root: Path | None = None) -> Path:
    base = Path.cwd() if root is None else Path(root)
    return base / ".agents" / SESSIONS_DIRNAME


def make_session_id(when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return stamp


def derive_title(messages: list[dict], fallback: str = "未命名会话") -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        text = str(message.get("content") or "").strip()
        if not text:
            continue
        if text.startswith("[AGENTS.md]") or text.startswith("[上下文压缩摘要]"):
            continue
        line = text.splitlines()[0].strip()
        line = re.sub(r"\s+", " ", line)
        if len(line) > 40:
            line = line[:40] + "…"
        return line or fallback
    return fallback


def save_session(
    record: SessionRecord,
    root: Path | None = None,
) -> Path:
    folder = sessions_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record.id}.json"
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_session(session_id: str, root: Path | None = None) -> SessionRecord:
    path = sessions_dir(root) / f"{session_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"会话不存在: {session_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("会话文件格式无效")
    return SessionRecord.from_dict(data)


def list_sessions(root: Path | None = None) -> list[SessionRecord]:
    folder = sessions_dir(root)
    if not folder.is_dir():
        return []
    records: list[SessionRecord] = []
    for path in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        record = SessionRecord.from_dict(data)
        if not record.id:
            record.id = path.stem
        records.append(record)
    return records
