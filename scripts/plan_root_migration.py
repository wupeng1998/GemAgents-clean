#!/usr/bin/env python3
"""Create a reversible, non-mutating plan for the N13 root-file migration.

The command only plans tracked root documents/configuration.  It never moves,
rewrites, deletes, or creates a destination copy.  Unknown references and any
KEEP decision from the N12 closure remain KEEP here as well.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout

SCHEMA_VERSION = 1
ROOT_TARGETS = {
    "findings.md": "docs/archive/initial-reconstruction/findings.md",
    "research-log.md": "docs/archive/initial-reconstruction/research-log.md",
    "research-state.yaml": "docs/status/research-state.snapshot.yaml",
    "iml1515_native_config.json": "docs/archive/legacy-configs/iml1515_native_config.json",
    "iml1515_native_v37_config.json": "docs/archive/legacy-configs/iml1515_native_v37_config.json",
    "native_faa_v2.json": "docs/archive/legacy-configs/native_faa_v2.json",
    "native_fna_v2.json": "docs/archive/legacy-configs/native_fna_v2.json",
}


def _raw_candidate(layout: RepoLayout, value: str | Path) -> Path:
    raw = Path(value).expanduser()
    return layout.root / raw if not raw.is_absolute() else raw


def _has_symlink_component(layout: RepoLayout, value: str | Path) -> bool:
    """Check a path lexically before resolve() can follow a symlink."""
    candidate = _raw_candidate(layout, value)
    try:
        relative = candidate.relative_to(layout.root)
    except ValueError:
        return candidate.is_symlink()
    current = layout.root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tracked(root: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _n12_plan(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("N12 plan must be an object")
    return payload


def build_plan(root: Path, *, n12_plan: Path | None = None) -> dict[str, Any]:
    layout = RepoLayout(root)
    n12_payload = _n12_plan(n12_plan)
    n12 = {
        str(row["path"]): row
        for row in n12_payload.get("entries", [])
        if isinstance(row, dict) and "path" in row
    }
    n12_blocked = bool(n12_payload.get("unresolved_references"))
    entries: list[dict[str, Any]] = []
    for source, destination in ROOT_TARGETS.items():
        source_path = _raw_candidate(layout, source)
        n12_row = n12.get(source, {})
        row: dict[str, Any] = {
            "source_path": source,
            "destination": destination,
            "restore_path": source,
            "action": "KEEP",
            "reason": "source_missing",
            "n12_reason": n12_row.get("reason"),
            "n12_action": n12_row.get("action"),
            "source_sha256": None,
            "size_bytes": None,
            "referrers": list(n12_row.get("referrers", [])),
            "pinned_by": list(n12_row.get("pinned_by", [])),
            "approval_required": True,
            "purge_authorized": False,
            "archive_verified": False,
            "restore_verified": False,
            "tracked": _tracked(layout.root, source),
        }
        if _has_symlink_component(layout, source):
            row["reason"] = "symlink_not_followed"
        elif source_path.is_file():
            row["source_sha256"] = sha256(source_path)
            row["size_bytes"] = source_path.stat().st_size
            row["reason"] = str(n12_row.get("reason", "unknown_reference_graph"))
            if _has_symlink_component(layout, destination):
                row["reason"] = "destination_symlink"
            elif n12_blocked:
                row["reason"] = "n12_unresolved_references"
            elif not row["tracked"]:
                row["reason"] = "untracked_source"
            else:
                destination_path = layout.resolve(destination)
                if destination_path.exists() and sha256(destination_path) != row["source_sha256"]:
                    row["reason"] = "destination_conflict"
                elif not n12_row:
                    row["reason"] = "not_in_n12_inventory"
                elif n12_row.get("action") == "ARCHIVE_COPY" and not n12_blocked:
                    row["action"] = "ARCHIVE_COPY"
                    row["reason"] = "approved_only_after_n12_revalidation"
        entries.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "root": str(layout.root),
        "source_policy": "N13 dry-run; no filesystem mutation",
        "n12_plan": str(n12_plan) if n12_plan else None,
        "n12_unresolved_references": n12_blocked,
        "n12_plan_id": n12_payload.get("plan_id"),
        "entries": entries,
        "apply_enabled": False,
        "rollback": "restore_path with matching source_sha256; never overwrite a newer file",
    }


def validate_plan(root: Path, plan: dict[str, Any]) -> list[str]:
    """Validate a dry-run plan without copying, moving, or deleting files."""
    layout = RepoLayout(root)
    problems: list[str] = []
    if plan.get("apply_enabled") is not False:
        problems.append("apply_must_remain_disabled")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        return [*problems, "entries_not_a_list"]
    destinations: set[str] = set()
    for index, row in enumerate(entries):
        if not isinstance(row, dict):
            problems.append(f"entry_not_object:{index}")
            continue
        action = row.get("action")
        if action not in {"KEEP", "ARCHIVE_COPY"}:
            problems.append(f"invalid_action:{index}")
        if action == "ARCHIVE_COPY":
            if plan.get("n12_unresolved_references"):
                problems.append(f"archive_blocked_by_n12:{index}")
            if row.get("n12_action") != "ARCHIVE_COPY":
                problems.append(f"archive_without_n12_approval:{index}")
            if row.get("approval_required") is not True:
                problems.append(f"approval_required_missing:{index}")
            if row.get("purge_authorized") is not False:
                problems.append(f"purge_authorization_invalid:{index}")
        source_name = row.get("source_path")
        destination_name = row.get("destination")
        if not isinstance(source_name, str) or not isinstance(destination_name, str):
            problems.append(f"entry_paths_missing:{index}")
            continue
        if destination_name in destinations:
            problems.append(f"duplicate_destination:{destination_name}")
        destinations.add(destination_name)
        source_symlink = _has_symlink_component(layout, source_name)
        destination_symlink = _has_symlink_component(layout, destination_name)
        if source_symlink:
            problems.append(f"source_symlink:{source_name}")
        if destination_symlink:
            problems.append(f"destination_symlink:{destination_name}")
        if source_symlink or destination_symlink:
            continue
        try:
            source = layout.resolve(source_name)
            destination = layout.resolve(destination_name)
        except ValueError:
            problems.append(f"path_escape:{source_name}")
            continue
        expected = row.get("source_sha256")
        if row.get("action") == "KEEP" and not source.exists():
            continue
        if not source.is_file():
            problems.append(f"source_missing:{source_name}")
        elif not isinstance(expected, str) or sha256(source) != expected:
            problems.append(f"source_hash_mismatch:{source_name}")
        if destination.exists() and destination.is_file():
            if not isinstance(expected, str) or sha256(destination) != expected:
                problems.append(f"destination_conflict:{destination_name}")
        elif destination.exists():
            problems.append(f"destination_not_file:{destination_name}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--n12-plan", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-plan", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.validate_plan is not None:
        plan_path = (
            args.validate_plan
            if args.validate_plan.is_absolute()
            else root / args.validate_plan
        )
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SystemExit(f"invalid migration plan: {plan_path}") from error
        problems = validate_plan(root, plan)
        print(json.dumps({"valid": not problems, "problems": problems}, ensure_ascii=False))
        return 0 if not problems else 1
    if args.output is None:
        parser.error("--output is required unless --validate-plan is provided")
    output = args.output if args.output.is_absolute() else root / args.output
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing plan: {output}")
    plan = build_plan(root, n12_plan=args.n12_plan)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "apply_enabled": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
