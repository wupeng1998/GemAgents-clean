#!/usr/bin/env python3
"""Run a read-only clean-snapshot and restore rehearsal.

The snapshot is materialized from ``git archive HEAD`` into a temporary
directory.  No checkout, branch, worktree, asset migration or repository file
is changed.  Runtime probes are skipped with an explicit environment status
when the active interpreter cannot satisfy the project's Python contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from GemAgents.layout import RepoLayout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_member(member: tarfile.TarInfo) -> bool:
    parts = Path(member.name).parts
    if not parts or any(part in {"", ".", "..", "mqc", "pear"} for part in parts):
        return False
    return member.isdir() or member.isreg()


def _materialize_snapshot(root: Path, destination: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("git archive HEAD failed")
    skipped = 0
    extracted = 0
    archive_path = destination / "snapshot.tar"
    archive_path.write_bytes(completed.stdout)
    try:
        with tarfile.open(archive_path, mode="r") as archive:
            for member in archive:
                if not _safe_member(member):
                    skipped += 1
                    continue
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read archived member: {member.name}")
                target.write_bytes(source.read())
                extracted += 1
    finally:
        archive_path.unlink(missing_ok=True)
    return {"extracted_files": extracted, "skipped_members": skipped}


def _restore_rehearsal(workspace: Path) -> dict[str, Any]:
    source = workspace / "restore-source.txt"
    restored = workspace / "restore-target.txt"
    source.write_text("clean-clone restore rehearsal\n", encoding="utf-8")
    expected = _sha256(source)
    shutil.copy2(source, restored)
    actual = _sha256(restored)
    return {
        "status": "PASS" if expected == actual else "FAIL",
        "source_sha256": expected,
        "restored_sha256": actual,
        "overwrite_policy": "copy_to_empty_destination_only",
    }


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "NOT_RUN",
        "head_sha": None,
        "snapshot": {},
        "runtime_probe": "NOT_RUN",
        "restore_rehearsal": "NOT_RUN",
        "mutations": [],
    }
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        result.update({"status": "FAIL", "reason": "not a git repository"})
        return result
    result["head_sha"] = head.stdout.strip()
    with tempfile.TemporaryDirectory(prefix="gemagents-clean-clone-") as raw:
        snapshot = Path(raw) / "snapshot"
        snapshot.mkdir()
        result["snapshot"] = _materialize_snapshot(root, snapshot)
        required = [snapshot / "README.md", snapshot / "pyproject.toml", snapshot / "src/GemAgents"]
        if not all(path.exists() for path in required):
            result.update({"status": "FAIL", "reason": "clean snapshot is incomplete"})
            return result
        result["restore_rehearsal"] = _restore_rehearsal(Path(raw))
        if result["restore_rehearsal"]["status"] != "PASS":
            result.update({"status": "FAIL", "reason": "restore hash mismatch"})
            return result
        if not ((3, 10) <= sys.version_info[:2] < (3, 12)):
            result.update(
                {
                    "status": "BLOCKED_ENVIRONMENT",
                    "runtime_probe": "BLOCKED_ENVIRONMENT",
                    "reason": "clean-clone runtime probe requires Python >=3.10,<3.12",
                }
            )
            return result
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        environment["PYTHONPATH"] = str(snapshot / "src")
        probe = subprocess.run(
            [sys.executable, "-c", "import GemAgents; print(GemAgents.__version__)"],
            cwd=snapshot,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        result["runtime_probe"] = {
            "status": "PASS" if probe.returncode == 0 else "FAIL",
            "returncode": probe.returncode,
            "stdout": probe.stdout,
            "stderr": probe.stderr,
        }
        result["status"] = result["runtime_probe"]["status"]
        if result["status"] == "FAIL":
            result["reason"] = "clean snapshot import probe failed"
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        output = RepoLayout(root).writable(args.output)
    except (PermissionError, ValueError) as error:
        raise SystemExit(f"invalid verifier output path: {args.output}") from error
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing verifier output: {output}")
    try:
        result = verify(root)
    except (OSError, RuntimeError, tarfile.TarError) as error:
        result = {"schema_version": 1, "status": "FAIL", "reason": type(error).__name__}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else (
        2 if result["status"] == "BLOCKED_ENVIRONMENT" else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
