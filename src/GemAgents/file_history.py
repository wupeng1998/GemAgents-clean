from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout


@dataclass(frozen=True)
class Snapshot:
    id: str
    original_path: Path
    snapshot_path: Path
    created_at: str


class FileHistory:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.root = RepoLayout(workspace).runtime / "file-history"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._snapshots = self._load_index()

    def snapshot(self, path: Path) -> Snapshot | None:
        if not path.exists():
            return None
        snapshot_id = uuid.uuid4().hex
        target = self.root / snapshot_id / "content"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        snapshot = Snapshot(
            id=snapshot_id,
            original_path=path.resolve(),
            snapshot_path=target,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._snapshots[snapshot_id] = snapshot
        self._save_index()
        return snapshot

    def restore(self, snapshot_id: str) -> str:
        snapshot = self._snapshots[snapshot_id]
        snapshot.original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot.snapshot_path, snapshot.original_path)
        return f"restored {self._display_path(snapshot.original_path)}"

    def list_snapshots(self) -> list[Snapshot]:
        return sorted(self._snapshots.values(), key=lambda item: item.created_at)

    def render_list(self) -> str:
        return "\n".join(
            f"{snapshot.id}\t{snapshot.created_at}\t{self._display_path(snapshot.original_path)}"
            for snapshot in self.list_snapshots()
        )

    def _load_index(self) -> dict[str, Snapshot]:
        if not self.index_path.is_file():
            return {}
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {}
        snapshots: dict[str, Snapshot] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            snapshot = Snapshot(
                id=str(item["id"]),
                original_path=Path(str(item["original_path"])),
                snapshot_path=Path(str(item["snapshot_path"])),
                created_at=str(item["created_at"]),
            )
            snapshots[snapshot.id] = snapshot
        return snapshots

    def _save_index(self) -> None:
        self.index_path.write_text(
            json.dumps(
                [
                    {
                        "id": snapshot.id,
                        "original_path": str(snapshot.original_path),
                        "snapshot_path": str(snapshot.snapshot_path),
                        "created_at": snapshot.created_at,
                    }
                    for snapshot in self.list_snapshots()
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)
