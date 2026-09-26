"""Small durable event/checkpoint store for local Agent runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from GemAgents.datetime_compat import UTC


def request_hash(prompt: str, model: str) -> str:
    encoded = json.dumps({"prompt": prompt, "model": model}, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RunCheckpoint:
    thread_id: str
    attempt_id: str
    request_sha256: str
    contract_id: str | None
    status: str
    stop_reason: str | None


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    thread_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    contract_id TEXT,
                    status TEXT NOT NULL,
                    stop_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (thread_id, seq),
                    FOREIGN KEY (thread_id) REFERENCES runs(thread_id)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    node TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (thread_id, seq),
                    FOREIGN KEY (thread_id) REFERENCES runs(thread_id)
                );
                """
            )

    def begin(
        self, thread_id: str, request_sha256: str, *, contract_id: str | None = None
    ) -> RunCheckpoint:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            if row is not None:
                if row["request_sha256"] != request_sha256:
                    raise ValueError("thread_id is already bound to another request")
                if (
                    contract_id is not None
                    and row["contract_id"] is not None
                    and row["contract_id"] != contract_id
                ):
                    raise ValueError("thread_id is already bound to another contract")
                return RunCheckpoint(
                    row["thread_id"],
                    row["attempt_id"],
                    row["request_sha256"],
                    row["contract_id"],
                    row["status"],
                    row["stop_reason"],
                )
            attempt_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, attempt_id, request_sha256, contract_id, "running", None, now),
            )
        return RunCheckpoint(thread_id, attempt_id, request_sha256, contract_id, "running", None)

    def append(self, thread_id: str, item: dict[str, object]) -> None:
        required = {"seq", "ts", "event"}
        if not required <= item.keys():
            raise ValueError("event requires seq, ts and event")
        payload = {
            key: value
            for key, value in item.items()
            if key not in required and key != "state"
        }
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT MAX(seq) FROM events WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            if previous is not None and int(item["seq"]) <= int(previous):
                raise ValueError("event sequence must increase monotonically")
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (
                    thread_id,
                    int(item["seq"]),
                    str(item["event"]),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    float(item["ts"]),
                ),
            )
            if item["event"] == "node_checkpoint":
                state = item.get("state")
                node = item.get("node")
                if not isinstance(state, dict) or not isinstance(node, str):
                    raise ValueError("node_checkpoint requires a node and state object")
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        int(item["seq"]),
                        node,
                        json.dumps(state, ensure_ascii=False, sort_keys=True),
                        float(item["ts"]),
                    ),
                )

    def latest_snapshot(self, thread_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT seq, node, state_json, timestamp FROM snapshots "
                "WHERE thread_id = ? ORDER BY seq DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "seq": row["seq"],
            "node": row["node"],
            "state": json.loads(row["state_json"]),
            "ts": row["timestamp"],
        }

    def finish(self, thread_id: str, stop_reason: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE runs SET status = ?, stop_reason = ?, updated_at = ? WHERE thread_id = ?",
                ("stopped", stop_reason, datetime.now(UTC).isoformat(), thread_id),
            ).rowcount
            if changed != 1:
                raise KeyError(f"unknown thread: {thread_id}")

    def events(self, thread_id: str, *, after_seq: int = 0) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, event, payload_json, timestamp FROM events "
                "WHERE thread_id = ? AND seq > ? ORDER BY seq",
                (thread_id, after_seq),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "ts": row["timestamp"],
                "event": row["event"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]
