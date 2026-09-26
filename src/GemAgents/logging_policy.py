"""Bounded, redacted JSONL events and conservative retention classification."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import resolve_workspace

SECRET_KEY = re.compile(r"(?:api[_-]?key|token|password|secret|credential)", re.I)
SECRET_VALUE = re.compile(
    r"(?P<name>api[_-]?key|token|password|secret|credential)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    re.I,
)
DEFAULT_MAX_BYTES = 64 * 1024
RETENTION_DAYS = {"scratch": 7, "debug": 30, "run": 90}


def _assert_no_symlink(path: Path, *, label: str) -> None:
    """Reject a path before filesystem operations can follow a symlink."""
    raw = path.expanduser()
    workspace = resolve_workspace()
    candidate = raw if raw.is_absolute() else workspace / raw
    current = Path(candidate.anchor) if candidate.anchor else workspace
    for component in candidate.relative_to(current).parts:
        current /= component
        if current.is_symlink():
            raise ValueError(f"{label} path must not contain a symlink")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub(
            lambda match: f"{match.group('name')}{match.group('separator')}[REDACTED]",
            value,
        )
    return value


def event_record(event: str, payload: dict[str, Any], *, sequence: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event": event,
        "sequence": sequence,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": redact(payload),
    }


def append_event(
    path: Path,
    event: str,
    payload: dict[str, Any],
    *,
    sequence: int,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if max_bytes < 128:
        raise ValueError("max_bytes must be at least 128")
    _assert_no_symlink(path, label="event")
    if path.exists():
        lines = path.read_bytes().splitlines()
        if lines:
            try:
                previous = json.loads(lines[-1].decode("utf-8"))["sequence"]
                previous = int(previous)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
                raise ValueError("existing event log has an invalid sequence") from error
            if sequence <= previous:
                raise ValueError("event sequence must increase monotonically")
    record = event_record(event, payload, sequence=sequence)
    encoded = _encode(record)
    if len(encoded) > max_bytes:
        original_bytes = len(encoded)
        record["payload"] = {"truncated": True, "original_bytes": original_bytes}
        encoded = _encode(record)
        if len(encoded) > max_bytes:
            # Keep the bounded-log guarantee even for an unusually long event name.
            record["event"] = "event_truncated"
            encoded = _encode(record)
        if len(encoded) > max_bytes:
            raise ValueError("max_bytes is too small for a bounded event record")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(encoded)
    return record


def _encode(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries_sha256(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tombstones_for_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive rollback records from the immutable entry set.

    Tombstones are evidence of what an approved copy would have to restore;
    callers must not be able to alter that evidence independently of the
    hash-bound entries.
    """
    tombstones: list[dict[str, Any]] = []
    for entry in entries:
        blocked = entry.get("status") == "BLOCKED" or entry.get("action") != "COPY"
        tombstones.append(
            {
                "source": entry.get("source"),
                "destination": entry.get("destination"),
                "source_sha256": entry.get("source_sha256"),
                "status": "PENDING_APPROVED_COPY" if blocked else "PENDING",
                "purge_enabled": False,
            }
        )
    return tombstones


def archive_copy(
    source: Path,
    destination: Path,
    *,
    active: bool = False,
    pinned: bool = False,
    approved: bool = False,
) -> dict[str, Any]:
    """Copy one immutable log to cold storage and verify it.

    This function never removes or replaces the source.  It also refuses to
    copy an active or pinned object, unless an explicit approved plan is
    supplied by the caller.  A same-hash destination is treated as an
    idempotent retry; a different destination is an error.
    """
    _assert_no_symlink(source, label="archive source")
    _assert_no_symlink(destination, label="archive destination")
    if not source.is_file():
        raise FileNotFoundError(source)
    if (active or pinned) and not approved:
        raise PermissionError("active or pinned objects require an approved archive plan")
    source_hash = _sha256(source)
    if destination.exists():
        if not destination.is_file() or _sha256(destination) != source_hash:
            raise FileExistsError(f"archive destination conflicts: {destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    archived_hash = _sha256(destination)
    if archived_hash != source_hash:
        raise OSError("archive copy hash verification failed")
    return {
        "source": str(source),
        "destination": str(destination),
        "source_sha256": source_hash,
        "archive_sha256": archived_hash,
        "archive_verified": True,
        "removable_copy": False,
        "purge_enabled": False,
    }


def restore_copy(manifest: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Restore a verified archive without overwriting a user file."""
    source = Path(str(manifest["destination"]))
    _assert_no_symlink(source, label="archive source")
    _assert_no_symlink(destination, label="restore destination")
    expected = str(manifest["archive_sha256"])
    if not source.is_file() or _sha256(source) != expected:
        raise OSError("archive source is missing or failed hash verification")
    if destination.exists():
        if _sha256(destination) == expected:
            return {"restored": False, "already_present": True, "sha256": expected}
        raise FileExistsError(f"restore destination conflicts: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if _sha256(destination) != expected:
        raise OSError("restore hash verification failed")
    return {"restored": True, "already_present": False, "sha256": expected}


def retention_class(
    path: Path,
    *,
    active: bool = False,
    pinned: bool = False,
    now: datetime | None = None,
) -> str:
    _assert_no_symlink(path, label="retention")
    if active or pinned:
        return "protected"
    now = now or datetime.now(UTC)
    age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    if age <= timedelta(days=7):
        return "hot"
    if age <= timedelta(days=90):
        return "queryable"
    return "cold_candidate"


def retention_policy(
    category: str, *, active: bool = False, pinned: bool = False
) -> dict[str, Any]:
    """Return a non-destructive retention recommendation for an object."""
    if category not in RETENTION_DAYS:
        raise ValueError(f"unknown retention category: {category}")
    return {
        "category": category,
        "review_after_days": RETENTION_DAYS[category],
        "class": "protected" if active or pinned else category,
        "purge_enabled": False,
        "requires_reference_check": True,
    }


def prepare_archive_manifest(
    entries: Iterable[dict[str, Any]],
    *,
    plan_id: str,
    approved: bool = False,
) -> dict[str, Any]:
    """Prepare a hash-bound, non-destructive archive/relocation manifest."""
    if not plan_id.strip():
        raise ValueError("archive plans require a plan_id")
    rows: list[dict[str, Any]] = []
    for item in entries:
        source = Path(str(item["source"]))
        destination = Path(str(item["destination"]))
        _assert_no_symlink(source, label="archive source")
        _assert_no_symlink(destination, label="archive destination")
        active = bool(item.get("active", False))
        pinned = bool(item.get("pinned", False))
        if not source.is_file():
            raise FileNotFoundError(source)
        digest = _sha256(source)
        blocked = (active or pinned) and not approved
        status = "BLOCKED" if blocked else "READY"
        reason = "active_or_pinned_requires_approval" if blocked else "ready_for_copy"
        rows.append(
            {
                "source": str(source),
                "destination": str(destination),
                "source_sha256": digest,
                "bytes": source.stat().st_size,
                "active": active,
                "pinned": pinned,
                "status": status,
                "action": "KEEP" if blocked else "COPY",
                "reason": reason,
            }
        )
    tombstones = _tombstones_for_entries(rows)
    return {
        "schema_version": 1,
        "plan_id": plan_id,
        "approved": approved,
        "purge_enabled": False,
        "entries_sha256": _entries_sha256(rows),
        "entries": rows,
        "tombstones": tombstones,
    }


def apply_archive_manifest(
    manifest: dict[str, Any], *, approved: bool = False
) -> dict[str, Any]:
    """Copy and verify READY entries only after explicit approval."""
    if not approved or not manifest.get("approved"):
        raise PermissionError("archive manifest requires explicit approval")
    if manifest.get("purge_enabled") is not False:
        raise PermissionError("archive manifest must keep purge disabled")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or manifest.get("entries_sha256") != _entries_sha256(
        entries
    ):
        raise ValueError("archive manifest integrity check failed")
    if manifest.get("tombstones") != _tombstones_for_entries(entries):
        raise ValueError("archive manifest tombstones integrity failed")
    results = []
    tombstones = []
    for entry in entries:
        if entry.get("status") != "READY" or entry.get("action") != "COPY":
            continue
        source = Path(str(entry["source"]))
        expected_source_hash = entry.get("source_sha256")
        if (
            not isinstance(expected_source_hash, str)
            or not source.is_file()
            or _sha256(source) != expected_source_hash
        ):
            raise OSError("archive source changed after manifest preparation")
        result = archive_copy(
            source,
            Path(str(entry["destination"])),
            active=bool(entry.get("active")),
            pinned=bool(entry.get("pinned")),
            approved=True,
        )
        results.append(result)
        tombstones.append(
            {
                "source": entry["source"],
                "destination": entry["destination"],
                "source_sha256": entry["source_sha256"],
                "status": "COPIED_VERIFIED",
                "purge_enabled": False,
            }
        )
    return {
        "schema_version": 1,
        "plan_id": manifest.get("plan_id"),
        "status": "APPLIED_VERIFIED",
        "purge_enabled": False,
        "results": results,
        "tombstones": tombstones,
    }
