#!/usr/bin/env python3
"""Generate a traceable offline release manifest and run a tiny smoke check."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def summarize_run(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_root = manifest_path.parent
    quality_path = run_root / "quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.is_file() else {}
    artifacts = {
        name: {
            "path": rel(root, run_root / name),
            "sha256": sha256(run_root / name),
            "status": "AVAILABLE" if (run_root / name).is_file() else "BLOCKED_ASSET",
        }
        for name in ("model.xml", "quality.json", "gapfill-report.json", "manifest.json")
    }
    return {
        "run": rel(root, run_root),
        "status": manifest.get("status", "unknown"),
        "execution_status": manifest.get("execution_status", "unknown"),
        "model_status": manifest.get("model_status", "unknown"),
        "validation_status": manifest.get("validation_status", {}),
        "quality_status": quality.get("status", "not_available"),
        "evaluation_track": manifest.get("evaluation_track", "unclassified"),
        "input_sha256": manifest.get("input_sha256"),
        "workflow_sha256": manifest.get("workflow_sha256"),
        "artifacts": artifacts,
    }


def capability_table(root: Path, runs_root: Path) -> list[dict[str, Any]]:
    rows = []
    if not runs_root.is_dir():
        return rows
    for path in sorted(runs_root.glob("*/manifest.json")):
        try:
            rows.append(summarize_run(root, path))
        except (OSError, ValueError, KeyError) as error:
            rows.append(
                {
                    "run": rel(root, path.parent),
                    "status": "BLOCKED_INVALID_MANIFEST",
                    "error_type": type(error).__name__,
                }
            )
    return rows


def code_identity(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def dirty_diff_sha256(root: Path) -> str | None:
    """Hash tracked dirty changes for cleanup-plan freshness checks."""
    try:
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return hashlib.sha256(diff).hexdigest()


def code_state(root: Path) -> dict[str, Any]:
    """Describe tracked changes and untracked names without reading untracked payloads."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        tracked_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {
            "head_sha": "unknown",
            "dirty": None,
            "tracked_diff_sha256": None,
            "untracked_paths_sha256": None,
            "untracked_content": None,
        }
    return {
        "head_sha": code_identity(root),
        "dirty": bool(status),
        "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
        "untracked_paths_sha256": hashlib.sha256(untracked).hexdigest(),
        "untracked_content": _untracked_content_hash(
            root, _git_paths(root, "ls-files", "--others", "--exclude-standard")
        ),
    }


def _git_paths(root: Path, *args: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", *args, "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


_CONTENT_HASH_PREFIXES = (
    ".github/",
    "benchmarks/",
    "constraints/",
    "docs/",
    "schemas/",
    "scripts/",
    "src/",
    "tests/",
)
_CONTENT_HASH_SUFFIXES = frozenset(
    {".json", ".md", ".py", ".rst", ".toml", ".txt", ".yaml", ".yml"}
)
_CONTENT_HASH_LIMIT = 8 * 1024 * 1024
_SENSITIVE_PATH_PARTS = frozenset(
    {".env", "secret", "secrets", "credential", "credentials", "token", "password", "passwd"}
)
_SENSITIVE_MARKERS = ("secret", "credential", "token", "password", "passwd")

RELEASE_PROFILES: dict[str, dict[str, object]] = {
    "engineering": {
        "version": "0.1.0-engineering",
        "stage": "engineering_candidate",
        "target_stage": "engineering_reproducible",
        "quality_constrained": "NOT_RUN",
        "phenotype_validated": False,
        "required_checks": ["offline_smoke"],
    },
}


def _restricted_reference(value: object) -> bool:
    """Keep restricted asset names out of generated release evidence."""
    if not isinstance(value, str):
        return False
    return any(
        part.lower() in {"mqc", "pear"}
        or part.lower().startswith(("mqc_", "mqc-", "pear_", "pear-"))
        for part in Path(value).parts
    )


def _untracked_content_hash(root: Path, paths: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256()
    included = 0
    skipped = 0
    for relative in sorted(paths):
        lowered = relative.lower()
        parts = set(Path(lowered).parts)
        if (
            not relative.startswith(_CONTENT_HASH_PREFIXES)
            or any(part in {"artifacts", "runs", "mqc", "pear"} for part in parts)
            or any(
                part in _SENSITIVE_PATH_PARTS
                or any(marker in part for marker in _SENSITIVE_MARKERS)
                or part.endswith((".pem", ".key"))
                for part in parts
            )
            or Path(relative).suffix.lower() not in _CONTENT_HASH_SUFFIXES
        ):
            skipped += 1
            continue
        path = root / relative
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > _CONTENT_HASH_LIMIT
            ):
                skipped += 1
                continue
            content = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
        included += 1
    return {
        "sha256": digest.hexdigest(),
        "files": included,
        "skipped": skipped,
        "policy": "small text under source/document prefixes; assets and restricted paths excluded",
    }


def _file_stats(root: Path, paths: list[str]) -> dict[str, int]:
    files = 0
    bytes_total = 0
    for relative in paths:
        path = root / relative
        try:
            stat = path.lstat()
        except OSError:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        files += 1
        bytes_total += stat.st_size
    return {"files": files, "bytes": bytes_total}


def _runtime_stats(root: Path) -> dict[str, int]:
    runtime = RepoLayout(root).runtime
    files = 0
    bytes_total = 0
    if not runtime.is_dir() or runtime.is_symlink():
        return {"files": 0, "bytes": 0}
    for base, directories, names in os.walk(runtime, followlinks=False):
        base_path = Path(base)
        directories[:] = [name for name in directories if not (base_path / name).is_symlink()]
        for name in names:
            path = base_path / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                bytes_total += path.stat().st_size
            except OSError:
                continue
            files += 1
    return {"files": files, "bytes": bytes_total}


def directory_cleanup_report(root: Path, cleanup_plan_path: Path | None = None) -> dict[str, Any]:
    """Build a read-only cleanup summary without deciding or applying deletions."""
    root = root.resolve()
    tracked = _git_paths(root, "ls-files")
    untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard")
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "NOT_PROVIDED",
        "source_plan": None,
        "plan_sha256": None,
        "classified": {
            "tracked": _file_stats(root, tracked),
            "untracked": _file_stats(root, untracked),
            "runtime": _runtime_stats(root),
        },
        "archived_count": 0,
        "unprocessed_count": 0,
        "protected_reference_count": 0,
        "protected_bytes": 0,
        "restricted_reference_count": 0,
        "unresolved_references": None,
        "rollback_entries": [],
        "unmet": ["cleanup_plan_not_provided"],
    }
    if cleanup_plan_path is None:
        return report
    plan_path = cleanup_plan_path.resolve()
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        report["status"] = "BLOCKED_INVALID_PLAN"
        report["unmet"] = [f"invalid_cleanup_plan:{type(error).__name__}"]
        return report
    if not isinstance(payload, dict) or payload.get("apply_enabled") is not False:
        report["status"] = "BLOCKED_INVALID_PLAN"
        report["unmet"] = ["cleanup_plan_apply_enabled"]
        return report
    plan_head = payload.get("head_sha")
    if plan_head is not None and plan_head != code_identity(root):
        report["status"] = "BLOCKED_STALE_PLAN"
        report["unmet"] = ["cleanup_plan_head_changed"]
        return report
    plan_dirty_diff = payload.get("dirty_diff_sha256")
    current_dirty_diff = dirty_diff_sha256(root)
    if current_dirty_diff is not None and plan_dirty_diff != current_dirty_diff:
        report["status"] = "BLOCKED_STALE_PLAN"
        report["unmet"] = ["cleanup_plan_dirty_diff_changed"]
        return report
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        report["status"] = "BLOCKED_INVALID_PLAN"
        report["unmet"] = ["cleanup_plan_entries_not_list"]
        return report
    protected = 0
    protected_bytes = 0
    archived = 0
    unprocessed = 0
    rollback_entries: list[dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        source = row.get("path") or row.get("source_path")
        if _restricted_reference(source):
            report["restricted_reference_count"] += 1
            continue
        action = row.get("action")
        if action in {"ARCHIVE_COPY", "ARCHIVED"}:
            archived += 1
        else:
            unprocessed += 1
        if row.get("referrers") or row.get("pinned_by") or row.get("active_job"):
            protected += 1
            protected_bytes += int(row.get("size_bytes") or 0)
        rollback_entries.append(
            {
                "path": row.get("path") or row.get("source_path"),
                "restore_path": row.get("restore_path") or row.get("source_path"),
                "destination": row.get("destination"),
                "action": action,
            }
        )
    report.update(
        {
            "status": "AVAILABLE",
            "source_plan": (
                str(plan_path.relative_to(root))
                if plan_path.is_relative_to(root)
                else str(plan_path)
            ),
            "plan_sha256": sha256(plan_path),
            "archived_count": archived,
            "unprocessed_count": unprocessed,
            "protected_reference_count": protected,
            "protected_bytes": protected_bytes,
            "unresolved_references": payload.get("unresolved_references"),
            "rollback_entries": rollback_entries,
            "unmet": (["unresolved_references"] if payload.get("unresolved_references") else []),
        }
    )
    return report


def build_release_manifest(
    root: Path,
    cleanup_plan_path: Path | None = None,
    *,
    profile: str = "engineering",
) -> dict[str, Any]:
    if profile not in RELEASE_PROFILES:
        raise ValueError(f"unknown release profile: {profile}")
    root = root.resolve()
    selected = [
        "README.md",
        "README.zh-CN.md",
        "AGENT.md",
        "AGENTS.md",
        "pyproject.toml",
        "environment.yml",
        "uv.lock",
        "benchmarks/protocol.yaml",
        "benchmarks/datasets.lock.json",
        "schemas/tool-spec.schema.json",
        "data/source-policy.yaml",
        "docs/benchmarks/results.md",
    ]
    assets = []
    for name in selected:
        path = root / name
        assets.append(
            {
                "path": name,
                "sha256": sha256(path),
                "status": "AVAILABLE" if path.is_file() else "BLOCKED_ASSET",
            }
        )
    declared_asset_status = (
        "PASS" if all(item["status"] == "AVAILABLE" for item in assets) else "BLOCKED"
    )
    return {
        "schema_version": 1,
        "release": {
            "profile": profile,
            **RELEASE_PROFILES[profile],
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "code_identity": code_identity(root),
        "code_state": code_state(root),
        "ci_run_id": None,
        "source_policy": {
            "path": "data/source-policy.yaml",
            "sha256": sha256(root / "data/source-policy.yaml"),
        },
        "gates": {
            "deterministic_core": "NOT_RUN",
            "uncached_certificate": "NOT_RUN",
            "external_phenotype_validation": "NOT_RUN",
            "dataset_status": "insufficient_data",
        },
        "verification": {
            "package_installation": {
                "status": "NOT_RUN",
                "evidence_refs": [],
                "reason": (
                    "The manifest generator does not install or validate a package "
                    "environment."
                ),
            },
            "declared_release_assets": {
                "status": declared_asset_status,
                "evidence_refs": [item["path"] for item in assets],
                "reason": (
                    "One or more declared release metadata files are missing."
                    if declared_asset_status != "PASS"
                    else "All declared release metadata files are present."
                ),
            },
            "full_reconstruction": {
                "status": "NOT_RUN",
                "evidence_refs": [],
                "reason": "This profile does not execute a full genome reconstruction.",
            },
            "online_agent": {
                "status": "NOT_RUN",
                "evidence_refs": [],
                "reason": "No online model/provider call is made by the offline release smoke.",
            },
            "phenotype_validation": {
                "status": "INSUFFICIENT_DATA",
                "evidence_refs": ["benchmarks/datasets.lock.json"],
                "reason": "The locked dataset is insufficient for biological advantage claims.",
            },
        },
        "assets": assets,
        "capabilities": capability_table(root, root / "runs"),
        "directory_cleanup": directory_cleanup_report(root, cleanup_plan_path),
        "known_limitations": [
            "The locked phenotype dataset is insufficient for biological advantage claims.",
            "External phenotype validation is not included in this engineering release.",
            (
                "Large reaction, biomass and annotation assets are referenced by hash "
                "and are not bundled."
            ),
            "Reference-assisted runs are not independent de_novo evidence.",
        ],
    }


def run_smoke(output: Path) -> dict[str, Any]:
    try:
        from cobra import Metabolite, Model, Reaction

        from GemAgents.metabolic.qc import Auditor, mass_probe

        model = Model("release_smoke")
        metabolite = Metabolite("a_c", compartment="c", formula="C", charge=0)
        other = Metabolite("b_c", compartment="c", formula="C", charge=0)
        reaction = Reaction("R", lower_bound=0, upper_bound=1)
        reaction.add_metabolites({metabolite: -1, other: 1})
        reverse = Reaction("R_reverse", lower_bound=0, upper_bound=1)
        reverse.add_metabolites({other: -1, metabolite: 1})
        model.add_reactions([reaction, reverse])
        result = Auditor(cache=False).audit(model, mass_probe(model))
        payload = {
            "status": "PASS" if result.status == "pass" else "FAIL",
            "probe_status": result.status,
            "semantic_scope": "offline_uncached_qc",
        }
    except (ImportError, ModuleNotFoundError) as error:
        # Missing optional scientific dependencies are an environment gate, not
        # a failed implementation.  Keep this distinct from a smoke assertion
        # or runtime defect so release evidence cannot hide a code regression.
        payload = {"status": "BLOCKED_ENVIRONMENT", "error_type": type(error).__name__}
    except Exception as error:
        payload = {"status": "FAIL", "error_type": type(error).__name__}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + chr(10), encoding="utf-8")
    return payload


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write(chr(10))


def _update_current_index(
    path: Path, manifest_path: Path, manifest: dict[str, Any], root: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        manifest_locator = rel(root, manifest_path)
    except ValueError:
        manifest_locator = str(manifest_path.resolve())
    pointer = {
        "schema_version": 1,
        "release_version": manifest["release"]["version"],
        "manifest": manifest_locator,
        "manifest_sha256": sha256(manifest_path),
        "code_identity": manifest["code_identity"],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(pointer, indent=2) + chr(10), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/releases/0.1.0-engineering/release-manifest.json"),
    )
    parser.add_argument(
        "--current-index",
        type=Path,
        default=Path("artifacts/releases/current.json"),
    )
    parser.add_argument("--smoke-output", type=Path, default=Path("artifacts/release/smoke.json"))
    parser.add_argument("--cleanup-plan", type=Path)
    parser.add_argument(
        "--profile",
        choices=tuple(RELEASE_PROFILES),
        default="engineering",
        help="Release gate profile to evaluate explicitly.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    cleanup_plan = (
        args.cleanup_plan
        if args.cleanup_plan is None or args.cleanup_plan.is_absolute()
        else root / args.cleanup_plan
    )
    manifest = build_release_manifest(root, cleanup_plan, profile=args.profile)
    smoke_path = (
        args.smoke_output
        if args.smoke_output.is_absolute()
        else root / args.smoke_output
    )
    smoke = run_smoke(smoke_path)
    gates = manifest.setdefault("gates", {})
    gates["offline_smoke"] = smoke.get("status", "NOT_RUN")
    required_checks = manifest["release"].get("required_checks", [])
    if not isinstance(required_checks, list) or any(
        gates.get(name) != "PASS" for name in required_checks
    ):
        print(json.dumps({"manifest_written": False, "smoke": smoke}, ensure_ascii=False))
        return 1
    output = args.output if args.output.is_absolute() else root / args.output
    try:
        _write_new_json(output, manifest)
    except FileExistsError:
        print(
            json.dumps(
                {
                    "manifest_written": False,
                    "error": "manifest_already_exists",
                    "path": str(output),
                },
                ensure_ascii=False,
            )
        )
        return 1
    current_index = (
        args.current_index
        if args.current_index.is_absolute()
        else root / args.current_index
    )
    if current_index:
        _update_current_index(current_index, output, manifest, root)
    try:
        displayed_output = rel(root, output)
    except ValueError:
        displayed_output = str(output.resolve())
    print(
        json.dumps(
            {"manifest": displayed_output, "current_index": str(current_index), "smoke": smoke},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
