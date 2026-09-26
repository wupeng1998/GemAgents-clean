#!/usr/bin/env python3
"""Create a conservative, read-only N00 inventory for the second upgrade round.

The inventory writes only to a new attempt directory.  It never follows
restricted paths or symlinks, never reads credential files, and refuses to
overwrite an existing output artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC

RESTRICTED_RE = re.compile(r"(?:^|[\\/_.-])(mqc|pear)(?:$|[\\/_.-])", re.I)
CREDENTIAL_RE = re.compile(
    r"(?:^|[._-])(\.env|secret|secrets|credential|credentials|token|password|passwd|api[_-]?key)(?:$|[._-])",
    re.I,
)
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
HASH_LIMIT_BYTES = 2 * 1024**3
TEXT_LIMIT_BYTES = 8 * 1024**2
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled", "finished"}
INTERESTING_IGNORED_ROOTS = {
    ".gemagents",
    "artifacts",
    "bigg",
    "carveme",
    "data",
    "experiments",
    "literature",
    "reconstructor",
    "runs",
}
PINNED_PARTS = (
    "release-manifest.json",
    "artifacts/audit",
    "benchmarks/datasets.lock.json",
    "benchmarks/protocol.yaml",
    "data/source-policy.yaml",
    "docs/upgrade/acceptance",
)
PATH_TOKEN_RE = re.compile(r"(?<![\w])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+")
ROOT_REFERENCE_NAMES = frozenset(
    {"findings.md", "research-log.md", "research-state.yaml"}
)
ROOT_REFERENCE_RE = re.compile(
    r"(?<![\w./-])(?:findings\.md|research-log\.md|research-state\.yaml)(?![\w./-])"
)
REFERENCE_PREFIXES = (
    ".gemagents/",
    "artifacts/",
    "benchmarks/",
    "bigg/",
    "carveme/",
    "configs/",
    "constraints/",
    "containers/",
    "data/",
    "docs/",
    "experiments/",
    "literature/",
    "reconstructor/",
    "runs/",
    "schemas/",
    "scripts/",
    "src/",
    "tests/",
)
GENERATED_OUTPUT_PREFIXES = (
    "artifacts/ci/",
    "artifacts/memote/",
    "artifacts/audit/round2/",
)
MAX_REFERENCES_TOTAL = 50_000
MAX_REFERENCES_PER_SOURCE = 10_000


def run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def restricted(relative_path: str) -> bool:
    return bool(RESTRICTED_RE.search(relative_path))


def credential_path(relative_path: str) -> bool:
    return any(CREDENTIAL_RE.search(part) for part in Path(relative_path).parts)


def git_path_set(root: Path, *args: str) -> set[str]:
    raw = run_git(root, *args, "-z")
    return {item for item in raw.split("\0") if item}


def ignored_entries(root: Path) -> set[str]:
    raw = run_git(root, "status", "--short", "--ignored", "--untracked-files=normal", "-z")
    entries: set[str] = set()
    for item in raw.split("\0"):
        if item.startswith("!! "):
            entries.add(item[3:].rstrip("/"))
    return entries


def expand_ignored(root: Path, entries: Iterable[str]) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    roots: set[str] = set()
    for item in entries:
        path = root / item
        first = Path(item).parts[0] if Path(item).parts else ""
        if path.is_dir() and first in INTERESTING_IGNORED_ROOTS:
            roots.add(item)
            for directory, dirnames, filenames in os.walk(path, followlinks=False):
                directory_path = Path(directory)
                dirnames[:] = [
                    name for name in dirnames if not (directory_path / name).is_symlink()
                ]
                for name in filenames:
                    files.add(relative(root, directory_path / name))
        elif path.is_file() or path.is_symlink():
            files.add(item)
        else:
            roots.add(item)
    return files, roots


def source_for(relative_path: str) -> str:
    if relative_path.startswith("runs/"):
        return "local historical or active run; producer not independently verified"
    if relative_path.startswith(".gemagents/"):
        return "local Agent runtime state; producer inferred from directory"
    if relative_path.startswith("artifacts/"):
        return "local task or validation artifact; producer inferred from path"
    if relative_path.startswith("data/"):
        return "local declared asset root; upstream provenance requires registry"
    if relative_path.startswith("GemAgents_Agent优化与目录治理_"):
        return "local review package"
    return "repository working tree"


def producer_for(relative_path: str) -> str:
    if relative_path.startswith("runs/"):
        return "metabolic pipeline or historical import (UNKNOWN)"
    if relative_path.startswith("artifacts/"):
        return "task command or validation script (inferred)"
    if relative_path.startswith(".gemagents/"):
        return "Agent runtime (inferred)"
    if relative_path.startswith("tests/"):
        return "repository test fixture or source"
    if relative_path.startswith("scripts/"):
        return "repository script"
    return "repository source or imported asset (UNKNOWN)"


def pinned(relative_path: str) -> bool:
    return any(
        relative_path == part or relative_path.startswith(part + "/")
        for part in PINNED_PARTS
    )


def job_snapshot(root: Path, relative_path: str) -> dict[str, Any] | None:
    if not relative_path.startswith(
        ".gemagents/metabolic_jobs/"
    ) or not relative_path.endswith(".json"):
        return None
    if relative_path.endswith((".contract.json", ".config.json")):
        # Sidecars are evidence linked to a primary job record, not jobs of
        # their own.  They remain protected through the structured reference
        # edges emitted by scan_references().
        return None
    path = root / relative_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"record_status": "UNKNOWN", "pid_alive": None, "record_parseable": False}
    if not isinstance(payload, dict):
        return {"record_status": "UNKNOWN", "pid_alive": None, "record_parseable": False}
    status = payload.get("status")
    pid = payload.get("pid")
    alive = None
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        except OSError:
            alive = False
        else:
            alive = True
    return {
        "record_status": status if isinstance(status, str) else "UNKNOWN",
        "pid": pid if isinstance(pid, int) else None,
        "pid_alive": alive,
        "record_parseable": True,
        "protect_until_revalidated": True,
        "observed_status": (
            "recoverable"
            if alive is False and status not in {"cancel_requested", "cancelling"}
            else "failed"
            if alive is False and status in {"cancel_requested", "cancelling"}
            else "running"
            if alive is True
            else "unknown"
        ),
        "recovery_reason": "exited" if alive is False else None,
    }


def inventory_paths(root: Path) -> tuple[set[str], set[str], set[str], set[str]]:
    tracked = git_path_set(root, "ls-files")
    untracked = git_path_set(root, "ls-files", "--others", "--exclude-standard")
    ignored = ignored_entries(root)
    ignored_files, ignored_roots = expand_ignored(root, ignored)
    paths = tracked | untracked | ignored_files | ignored_roots
    if not paths:
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            dirnames[:] = [name for name in dirnames if name != ".git"]
            paths.add(relative(root, directory_path))
            paths.update(relative(root, directory_path / name) for name in filenames)
    paths = {
        item
        for item in paths
        if item and not item.startswith(".git/") and item != ".git"
    }
    return paths, tracked, untracked, ignored_roots | ignored


def git_state(path: str, tracked: set[str], untracked: set[str], ignored: set[str]) -> str:
    if path in tracked:
        return "tracked"
    if path in untracked:
        return "untracked"
    if path in ignored:
        return "ignored"
    for prefix in ignored:
        if path.startswith(prefix.rstrip("/") + "/"):
            return "ignored"
    return "unknown"


def inspect_path(
    root: Path,
    path_text: str,
    tracked: set[str],
    untracked: set[str],
    ignored: set[str],
) -> dict[str, Any]:
    path = root / path_text
    record: dict[str, Any] = {
        "path": path_text,
        "git_state": git_state(path_text, tracked, untracked, ignored),
        "source": source_for(path_text),
        "producer": producer_for(path_text),
        "pinned": pinned(path_text),
        "sha256": None,
        "size_bytes": None,
        "status": "UNKNOWN",
        "classification": "unknown",
        "referrers": [],
    }
    if restricted(path_text):
        record.update({"status": "BLOCKED_POLICY", "classification": "restricted_external_project"})
        return record
    if credential_path(path_text):
        record.update(
            {
                "status": "PROTECTED_CREDENTIAL_METADATA_ONLY",
                "classification": "credential_or_secret_path",
                "content_read": False,
            }
        )
        try:
            record["size_bytes"] = path.lstat().st_size
        except OSError:
            pass
        return record
    try:
        stat = path.lstat()
    except OSError as error:
        record.update({"status": "UNKNOWN_REFERENCE", "error_type": type(error).__name__})
        return record
    record["size_bytes"] = stat.st_size
    if path.is_symlink():
        target = path.resolve(strict=False)
        record.update(
            {
                "status": (
                    "BLOCKED_SYMLINK_ESCAPE"
                    if root not in target.parents and target != root
                    else "BLOCKED_SYMLINK"
                ),
                "classification": "symlink_not_followed",
                "symlink_target": os.readlink(path),
                "symlink_within_root": target == root or root in target.parents,
                "content_read": False,
            }
        )
        return record
    if path.is_dir():
        record.update(
            {
                "status": "DIRECTORY_ONLY",
                "classification": "directory_entry",
                "content_read": False,
            }
        )
        return record
    if stat.st_size == 0:
        record["status"] = "AVAILABLE_WITH_FINDINGS"
        record["classification"] = "zero_byte_file"
    elif stat.st_size > HASH_LIMIT_BYTES:
        record.update({"status": "BLOCKED_ASSET", "classification": "oversize_not_hashed"})
        return record
    else:
        record["sha256"] = sha256_file(path)
        record["status"] = "AVAILABLE" if record["sha256"] else "BLOCKED_READ"
        record["classification"] = "ordinary_file"
    job = job_snapshot(root, path_text)
    if job:
        record["job"] = job
        record["classification"] = "job_record"
        if job.get("record_status") not in TERMINAL_JOB_STATES or job.get("pid_alive"):
            record["active_or_unknown_job"] = True
    return record


def reference_source_allowed(path_text: str) -> bool:
    if path_text.startswith("artifacts/audit/round2/"):
        # Round2 attempts are pinned evidence roots.  Their generated
        # manifests repeat historical paths and would inflate the closure
        # without changing any migration decision.
        return False
    if path_text in {"README.md", "README.zh-CN.md", "AGENT.md", "AGENTS.md"}:
        return True
    if path_text.startswith("runs/"):
        return Path(path_text).name in {"manifest.json", "accepted.json", "checkpoint.json"}
    if path_text.startswith("artifacts/"):
        return Path(path_text).name in {
            "release-manifest.json",
            "assets.lock.json",
            "historical-run-index.json",
            "datasets.lock.json",
        } or "/acceptance/" in path_text
    if path_text.startswith(".gemagents/"):
        return path_text.startswith(".gemagents/metabolic_jobs/") and path_text.endswith(".json")
    return path_text.startswith(
        (
            ".github/",
            "benchmarks/",
            "configs/",
            "constraints/",
            "containers/",
            "data/",
            "docs/",
            "scripts/",
            "schemas/",
            "src/",
            "tests/",
        )
    )


def _job_field_paths(payload: Any) -> list[str]:
    """Extract path-like values from a job record without opening targets."""
    values: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and (
            key.lower().endswith(("path", "file", "output", "log", "checkpoint"))
            or value.startswith(REFERENCE_PREFIXES)
            or value.startswith("/")
        ):
            values.append(value)

    visit(payload)
    return values


def _normalize_path_token(raw: str) -> str:
    """Normalize escaped path separators without corrupting ``\\runs``."""
    token = raw
    if token.endswith("\\n") or token.endswith("\\r"):
        token = token[:-2]
    return token.replace("\\", "/").strip("`'\".,;:()[]{}")


def _is_generated_output(token: str) -> bool:
    return any(
        token == prefix.rstrip("/") or token.startswith(prefix)
        for prefix in GENERATED_OUTPUT_PREFIXES
    )


def _reference_reason(source: str, target: str | None, status: str) -> str:
    if status == "BLOCKED_EXTERNAL_PATH":
        return "external_path_policy"
    if status == "BLOCKED_POLICY":
        return "restricted_asset_policy"
    if status == "BLOCKED_SYMLINK_ESCAPE":
        return "symlink_escape_policy"
    if status == "GENERATED_OUTPUT":
        return (
            "generated_audit_evidence"
            if target and target.startswith("artifacts/audit/round2/")
            else "generated_ci_output"
        )
    if status == "PRESENT":
        return "resolved"
    if status != "UNKNOWN_REFERENCE":
        return "unclassified"
    target_text = target or ""
    def starts_root(*roots: str) -> bool:
        return any(target_text == root or target_text.startswith(root + "/") for root in roots)

    if starts_root("artifacts/audit/round2"):
        return "generated_audit_evidence"
    if target_text.startswith("runs/") or source.startswith("artifacts/audit/"):
        return "historical_run_reference"
    if source.endswith("assets.lock.json") or starts_root(
        "bigg", "data/bigg", "data/inputs", "data/registry"
    ):
        return "declared_external_asset"
    if starts_root("data/reaction_library", "data/VERSION"):
        return "declared_local_asset"
    if any(marker in target_text for marker in ("input-", "universe_", "...", "<", ">")):
        return "dynamic_template"
    if starts_root(
        "docs/archive",
        "docs/status",
        "artifacts/archive",
        "artifacts/releases",
        "artifacts/index",
    ):
        return "planned_destination"
    if target_text == "scripts/pgap.py":
        return "external_dependency"
    if source.startswith("tests/"):
        return "test_fixture_placeholder"
    if source.startswith("docs/"):
        return "documentation_example"
    return "missing_local_reference"


def _structured_reference(
    root: Path,
    source: str,
    raw: str,
    by_path: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    token = _normalize_path_token(raw)
    if re.match(r"^[A-Za-z]:[/\\]", token):
        return {
            "source": source,
            "line": 0,
            "raw": raw,
            "normalized": token,
            "target": None,
            "status": "BLOCKED_EXTERNAL_PATH",
            "kind": "job_field",
            "reason": "external_path_policy",
        }
    if token.startswith("/"):
        candidate = Path(token).resolve(strict=False)
        try:
            target = relative(root, candidate)
        except ValueError:
            return {
                "source": source,
                "line": 0,
                "raw": raw,
                "normalized": token,
                "target": None,
                "status": "BLOCKED_EXTERNAL_PATH",
                "kind": "job_field",
                "reason": "external_path_policy",
            }
    elif token.startswith(REFERENCE_PREFIXES):
        candidate = (root / token).resolve(strict=False)
        try:
            target = relative(root, candidate)
        except ValueError:
            return {
                "source": source,
                "line": 0,
                "raw": raw,
                "normalized": token,
                "target": None,
                "status": "BLOCKED_SYMLINK_ESCAPE",
                "kind": "job_field",
                "reason": "symlink_escape_policy",
            }
    else:
        return {}
    status = "PRESENT" if candidate.exists() else "UNKNOWN_REFERENCE"
    if target in by_path:
        by_path[target]["referrers"].append(source)
    return {
        "source": source,
        "line": 0,
        "raw": raw,
        "normalized": token,
        "target": target,
        "status": status,
        "kind": "job_field",
        "reason": _reference_reason(source, target, status),
    }


def scan_references(
    root: Path, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_path = {item["path"]: item for item in records}
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    source_counts: dict[str, int] = defaultdict(int)
    truncated_sources: set[str] = set()
    for record in records:
        path = root / record["path"]
        if record["status"] in {"BLOCKED_POLICY", "PROTECTED_CREDENTIAL_METADATA_ONLY"}:
            continue
        if not reference_source_allowed(record["path"]):
            continue
        if (
            not path.is_file()
            or path.stat().st_size > TEXT_LIMIT_BYTES
            or path.suffix.lower() not in TEXT_SUFFIXES
        ):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        stop_source = False
        for line_number, line in enumerate(lines, 1):
            if stop_source or len(references) >= MAX_REFERENCES_TOTAL:
                break
            for raw in [*PATH_TOKEN_RE.findall(line), *ROOT_REFERENCE_RE.findall(line)]:
                if len(references) >= MAX_REFERENCES_TOTAL:
                    break
                token = _normalize_path_token(raw)
                if not token or token.startswith("http"):
                    continue
                if not (
                    token.startswith("/")
                    or re.match(r"^[A-Za-z]:[/\\]", token)
                    or token.startswith(REFERENCE_PREFIXES)
                    or token in ROOT_REFERENCE_NAMES
                ):
                    continue
                if source_counts[record["path"]] >= MAX_REFERENCES_PER_SOURCE:
                    truncated_sources.add(record["path"])
                    stop_source = True
                    break
                key = (record["path"], token, line_number)
                if key in seen:
                    continue
                seen.add(key)
                source_counts[record["path"]] += 1
                if token.startswith("/") or re.match(r"^[A-Za-z]:[/\\]", token):
                    status = "BLOCKED_EXTERNAL_PATH"
                    target = None
                elif restricted(token):
                    status = "BLOCKED_POLICY"
                    target = None
                elif _is_generated_output(token):
                    # CI output paths are created by the workflow and need not
                    # exist in a source checkout; they are not asset references.
                    status, target = "GENERATED_OUTPUT", token
                else:
                    candidate = (root / token).resolve(strict=False)
                    try:
                        candidate.relative_to(root)
                    except ValueError:
                        status, target = "BLOCKED_SYMLINK_ESCAPE", None
                    else:
                        target = relative(root, candidate)
                        status = "PRESENT" if candidate.exists() else "UNKNOWN_REFERENCE"
                        if target in by_path:
                            by_path[target]["referrers"].append(record["path"])
                references.append(
                    {
                        "source": record["path"],
                        "line": line_number,
                        "raw": raw,
                        "normalized": token,
                        "target": target,
                        "status": status,
                        "reason": _reference_reason(record["path"], target, status),
                    }
                )
        if record["path"].startswith(".gemagents/metabolic_jobs/"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                for raw in _job_field_paths(payload):
                    reference = _structured_reference(root, record["path"], raw, by_path)
                    if reference and len(references) < MAX_REFERENCES_TOTAL:
                        references.append(reference)
        if len(references) >= MAX_REFERENCES_TOTAL:
            break
    return references, {
        "max_total": MAX_REFERENCES_TOTAL,
        "max_per_source": MAX_REFERENCES_PER_SOURCE,
        "truncated": bool(truncated_sources) or len(references) >= MAX_REFERENCES_TOTAL,
        "truncated_sources": sorted(truncated_sources),
    }


def no_manifest_directories(root: Path) -> list[str]:
    findings: list[str] = []
    for parent_name in ("runs", ".gemagents/sessions"):
        parent = root / parent_name
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and not (child / "manifest.json").is_file():
                findings.append(relative(root, child))
    return findings


def environment(root: Path) -> dict[str, Any]:
    package_names = (
        "cobra",
        "jsonschema",
        "langchain-core",
        "langgraph",
        "memote",
        "optlang",
        "psutil",
        "pyhmmer",
        "pyrodigal",
        "pytest",
        "ruff",
        "swiglpk",
    )
    packages: dict[str, str | None] = {}
    for name in package_names:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    solvers: list[str] = []
    try:
        from cobra.util.solver import solvers as cobra_solvers

        solvers = sorted(cobra_solvers)
    except Exception:
        solvers = []
    executables = {name: shutil.which(name) for name in ("docker", "glpsol", "pgap", "wsl")}
    locks = {}
    lock_paths = [root / "pyproject.toml", root / "uv.lock", root / "environment.yml"]
    lock_paths.extend(sorted((root / "constraints").glob("*")))
    for path in lock_paths:
        if path.is_file() and not restricted(relative(root, path)):
            locks[relative(root, path)] = {
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": packages,
        "cobra_solvers": solvers,
        "executables": executables,
        "lockfiles": locks,
    }


def baseline(root: Path, paths: list[str]) -> dict[str, Any]:
    status = run_git(root, "status", "--porcelain=v1")
    diff_stat = run_git(root, "diff", "--stat")
    diff = run_git(root, "diff", "--binary")
    dirty_material = (status + "\n" + diff_stat + "\n" + diff).encode()
    authorized = []
    for name in ("data", "bigg", "carveme", "reconstructor", "runs", "artifacts", ".gemagents"):
        path = root / name
        authorized.append(
            {
                "path": name,
                "exists": path.exists(),
                "status": "RESTRICTED_NOT_OPENED" if restricted(name) else "LOCAL_SCOPE",
            }
        )
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "head_sha": run_git(root, "rev-parse", "HEAD").strip() or "UNKNOWN",
        "branch": run_git(root, "branch", "--show-current").strip() or "UNKNOWN",
        "dirty": bool(status.strip()),
        "dirty_diff_sha256": sha256_bytes(dirty_material),
        "dirty_status_porcelain": status.splitlines(),
        "dirty_diff_stat": diff_stat.splitlines(),
        "environment": environment(root),
        "authorized_asset_roots": authorized,
        "candidate_path_count": len(paths),
        "source_policy": "MQC and external pear paths are not opened or hashed",
    }


def build_inventory(root: Path) -> dict[str, Any]:
    paths, tracked, untracked, ignored = inventory_paths(root)
    records = [inspect_path(root, path, tracked, untracked, ignored) for path in sorted(paths)]
    references, reference_scan_limits = scan_references(root, records)
    basename_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("sha256"):
            basename_groups[Path(record["path"]).name].append(record)
    collisions = []
    for basename, group in sorted(basename_groups.items()):
        hashes = {item["sha256"] for item in group}
        if len(hashes) > 1:
            collisions.append(
                {
                    "basename": basename,
                    "paths": [item["path"] for item in group],
                    "sha256": sorted(hashes),
                    "classification": "different_content_same_name",
                }
            )
    status_counts: dict[str, int] = defaultdict(int)
    for record in records:
        status_counts[record["status"]] += 1
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "scope": "tracked, untracked and conservatively expanded ignored project paths",
        "policy": {
            "restricted_paths_not_opened": True,
            "credential_content_not_read": True,
            "symlinks_not_followed": True,
            "max_hash_bytes": HASH_LIMIT_BYTES,
        },
        "records": records,
        "references": references,
        "reference_scan_limits": reference_scan_limits,
        "same_name_different_content": collisions,
        "no_manifest_directories": no_manifest_directories(root),
        "summary": {
            "records": len(records),
            "status_counts": dict(sorted(status_counts.items())),
            "tracked": sum(item["git_state"] == "tracked" for item in records),
            "untracked": sum(item["git_state"] == "untracked" for item in records),
            "ignored_or_unknown": sum(
                item["git_state"] in {"ignored", "unknown"} for item in records
            ),
            "references": len(references),
            "same_name_collisions": len(collisions),
        },
    }


def cleanup_plan(inventory: dict[str, Any], policy_sha256: str | None) -> dict[str, Any]:
    entries = []
    action_counts: dict[str, int] = defaultdict(int)
    for record in inventory["records"]:
        if record.get("status") in {"BLOCKED_POLICY", "PROTECTED_CREDENTIAL_METADATA_ONLY"}:
            action, reason = "KEEP", "policy-protected; content was not opened"
        elif record.get("active_or_unknown_job"):
            action, reason = "KEEP", "active or stale job record requires live revalidation"
        elif record.get("pinned"):
            action, reason = "KEEP", "pinned evidence or release input"
        elif record.get("status") in {"UNKNOWN_REFERENCE", "BLOCKED_READ", "BLOCKED_ASSET"}:
            action, reason = "REVIEW", "unknown, unreadable or oversized; no automatic action"
        elif record.get("git_state") == "ignored":
            action, reason = "REVIEW", "ignored does not imply disposable"
        else:
            action, reason = "KEEP", "ordinary source or input; no approved relocation"
        action_counts[action] += 1
        entries.append(
            {
                "path": record["path"],
                "action": action,
                "reason": reason,
                "source_sha256": record.get("sha256"),
                "referrers": record.get("referrers", []),
                "active_job": record.get("job"),
            }
        )
    return {
        "schema_version": 1,
        "mode": "DRY_RUN",
        "default_action": "KEEP",
        "policy_sha256": policy_sha256,
        "approved_cleanup_manifest_sha256": None,
        "destructive_actions": [],
        "action_counts": dict(sorted(action_counts.items())),
        "entries": entries,
    }


def write_new(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing attempt artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(root: Path, output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory = build_inventory(root)
    base = baseline(root, [item["path"] for item in inventory["records"]])
    policy = root / (
        "GemAgents_Agent优化与目录治理_20260919/"
        "GemAgents_next_20260919/CLEANUP_POLICY.proposed.yaml"
    )
    policy_hash = sha256_file(policy) if policy.is_file() else None
    historical = run_git(root, "rev-parse", "HEAD")
    write_new(output_dir / "baseline.json", base)
    write_new(output_dir / "asset-inventory.json", inventory)
    write_new(output_dir / "cleanup-dry-run.json", cleanup_plan(inventory, policy_hash))
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.index_historical_runs import build_index

        history = build_index()
    except (ImportError, OSError, ValueError) as error:
        history = {
            "schema_version": 1,
            "scope": "history index unavailable",
            "error_type": type(error).__name__,
            "runs": [],
        }
    history["round2_head_sha"] = historical.strip() or "UNKNOWN"
    history["historical_status_not_revalidated"] = True
    write_new(output_dir / "historical-run-index.json", history)
    write_new(
        output_dir / "reference-index.json",
        {
            "schema_version": 1,
            "scan_limits": inventory["reference_scan_limits"],
            "references": inventory["references"],
        },
    )
    return {"output_dir": relative(root, output_dir), "summary": inventory["summary"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    result = run(root, args.output_dir if args.output_dir.is_absolute() else root / args.output_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
