#!/usr/bin/env python3
"""Record third-party and large-input ownership without relocating assets.

This inventory intentionally reads directory metadata and marker filenames only.
It does not open sequence, model, database, or literature payloads.  Unknown
licence, persistence, or restore state therefore stays explicitly blocked.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout

TARGETS = {
    "carveme": {
        "kind": "vendor_source",
        "destination": "third_party/carveme",
        "license_markers": ("LICENSE", "COPYING"),
    },
    "reconstructor": {
        "kind": "vendor_source",
        "destination": "third_party/reconstructor",
        "license_markers": ("LICENSE", "COPYING"),
    },
    "bigg": {
        "kind": "input_asset",
        "destination": "data/inputs/bigg",
        "license_markers": ("LICENSE", "README", "README.md"),
    },
    "literature": {
        "kind": "retrieval_material",
        "destination": "data/registry/literature",
        "license_markers": ("LICENSE", "README", "README.md"),
    },
}
ACCESSION = re.compile(r"(?:GCF|GCA)_\d+(?:\.\d+)?", re.I)
RESTRICTED_COMPONENT = re.compile(r"^(?:mqc|pear)$", re.I)


def _restricted(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(RESTRICTED_COMPONENT.fullmatch(part) for part in relative.parts)


def _metadata(root: Path, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    source = root / name
    source_symlink = source.is_symlink()
    marker_names = (
        {item.name for item in source.iterdir()}
        if source.is_dir() and not source_symlink
        else set()
    )
    files = 0
    directories = 0
    restricted_entries = 0
    symlink_entries = 0
    accession_hints: set[str] = set()
    if source.is_dir() and not source_symlink:
        for item in source.rglob("*"):
            if _restricted(item, root):
                restricted_entries += 1
                continue
            if item.is_symlink():
                symlink_entries += 1
                continue
            if item.is_dir():
                directories += 1
            elif item.is_file():
                files += 1
                accession_hints.update(ACCESSION.findall(item.name))
    license_markers = sorted(set(spec["license_markers"]) & marker_names)
    return {
        "source_path": name,
        "kind": spec["kind"],
        "proposed_destination": spec["destination"],
        "exists": source.exists(),
        "source_symlink": source_symlink,
        "source_file_count": files,
        "source_directory_count": directories,
        "restricted_entries_skipped": restricted_entries,
        "symlink_entries_skipped": symlink_entries,
        "top_level_marker_names": sorted(marker_names),
        "license_markers": license_markers,
        "license_status": "marker_present_review_required" if license_markers else "UNKNOWN",
        "accession_hints": sorted(accession_hints),
        "content_hash_status": "NOT_COMPUTED_PAYLOAD_NOT_READ",
        "upstream_commit": "UNKNOWN",
        "persistence_restore_status": "UNKNOWN",
        "migration_action": "KEEP",
        "reason": "symlink_not_followed" if source_symlink else "license_or_persistence_unverified",
        "approval_required": True,
        "purge_authorized": False,
    }


def build_inventory(root: Path) -> dict[str, Any]:
    layout = RepoLayout(root)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(layout.root),
        "source_policy": (
            "metadata-only; payloads not opened; MQC/pear path components skipped"
        ),
        "assets": [_metadata(layout.root, name, spec) for name, spec in TARGETS.items()],
        "migration_enabled": False,
        "note": (
            "Unknown licence, upstream identity, persistence, hash, and restore "
            "evidence remain blocked."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    layout = RepoLayout(root)
    try:
        output = layout.writable(args.output)
    except (PermissionError, ValueError) as error:
        raise SystemExit(f"invalid inventory output path: {args.output}") from error
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing inventory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_inventory(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "migration_enabled": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
