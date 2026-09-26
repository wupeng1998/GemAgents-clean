"""Small deterministic file primitives shared by metabolic stages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def metabolic_json(path: Path, value: object) -> None:
    """Atomically write a manifest/status; never include provider credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def metabolic_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
