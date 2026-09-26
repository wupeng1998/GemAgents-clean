"""Read-only resolution of historical artifact paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from GemAgents.layout import RepoLayout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid relocation index: {path}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("relocations", []), list):
        raise ValueError("relocation index must contain a relocations list")
    return payload


def resolve_artifact(
    root: Path,
    locator: str,
    *,
    index_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve an old relative path without following external paths."""
    layout = RepoLayout(root)
    try:
        original = layout.resolve(locator)
    except ValueError:
        return {"status": "BLOCKED_PATH_ESCAPE", "locator": locator}
    if original.is_file() and not original.is_symlink():
        return {
            "status": "PRESENT",
            "locator": locator,
            "path": str(original.relative_to(layout.root)),
            "sha256": _sha256(original),
            "via": "original_path",
        }
    index = index_path or layout.resolve("artifacts/index/relocations.json")
    if not index.is_file():
        return {"status": "UNRESOLVED", "locator": locator, "via": "no_relocation_index"}
    payload = _load_index(index)
    matches = [
        row
        for row in payload["relocations"]
        if isinstance(row, dict) and row.get("old_path") == locator
    ]
    if len(matches) != 1:
        return {"status": "UNRESOLVED", "locator": locator, "via": "relocation_index"}
    row = matches[0]
    current = row.get("current_location")
    expected = row.get("source_sha256")
    if not isinstance(current, str) or not isinstance(expected, str):
        raise ValueError("relocation row requires current_location and source_sha256")
    try:
        target = layout.resolve(current)
    except ValueError:
        return {"status": "BLOCKED_PATH_ESCAPE", "locator": locator}
    if not target.is_file() or target.is_symlink():
        return {"status": "MISSING_TARGET", "locator": locator, "path": current}
    actual = _sha256(target)
    if actual != expected:
        return {
            "status": "HASH_MISMATCH",
            "locator": locator,
            "path": current,
            "expected_sha256": expected,
            "actual_sha256": actual,
        }
    return {
        "status": "RESOLVED",
        "locator": locator,
        "path": current,
        "sha256": actual,
        "artifact_id": row.get("artifact_id"),
        "via": "relocation_index",
    }
