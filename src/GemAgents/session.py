from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout


@dataclass
class Session:
    id: str
    created_at: str
    messages: list[dict[str, Any]] = field(default_factory=list)


class SessionStore:
    def __init__(self, workspace: Path) -> None:
        self.root = RepoLayout(workspace).runtime / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> Session:
        return Session(id=uuid.uuid4().hex, created_at=datetime.now(UTC).isoformat())

    def latest_id(self) -> str | None:
        files = sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime)
        return files[-1].stem if files else None

    def load(self, session_id: str | None = None) -> Session:
        actual_id = session_id or self.latest_id()
        if actual_id is None:
            return self.create()
        if not re.fullmatch(r"[0-9a-f]{32}", actual_id):
            raise ValueError("invalid session ID")
        path = self.root / f"{actual_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return Session(
            id=str(data["id"]),
            created_at=str(data["created_at"]),
            messages=list(data.get("messages", [])),
        )

    def save(self, session: Session) -> None:
        path = self.root / f"{session.id}.json"
        path.write_text(
            json.dumps(
                {"id": session.id, "created_at": session.created_at, "messages": session.messages},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def list(self) -> list[str]:
        return [path.stem for path in sorted(self.root.glob("*.json"))]

