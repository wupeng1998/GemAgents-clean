#!/usr/bin/env python3
"""Generate a conservative repository plan from an existing inventory snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from GemAgents.layout import RepoLayout

POLICY_VERSION = "round2-n12-v2"
REVIEW_ARCHIVE_PATHS = frozenset({"findings.md", "research-log.md"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_candidate(layout: RepoLayout, value: str | Path) -> Path:
    raw = Path(value).expanduser()
    return layout.root / raw if not raw.is_absolute() else raw


def _has_symlink_component(layout: RepoLayout, value: str | Path) -> bool:
    """Inspect path components before resolve() can follow a symlink."""
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


def _head(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _dirty_diff_sha256(root: Path) -> str | None:
    """Hash tracked worktree changes so a plan cannot outlive dirty code."""
    try:
        diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--"], cwd=root)
    except (OSError, subprocess.CalledProcessError):
        return None
    return _sha256(diff)


def _reference_closure(inventory: dict[str, Any]) -> set[str]:
    graph: dict[str, set[str]] = {}
    for ref in inventory.get("references", []):
        source, target = ref.get("source"), ref.get("target")
        if isinstance(source, str) and isinstance(target, str):
            graph.setdefault(source, set()).add(target)
    protected = {
        str(row["path"])
        for row in inventory.get("records", [])
        if row.get("pinned") or row.get("status") != "AVAILABLE"
    }
    pending = list(protected)
    while pending:
        for target in graph.get(pending.pop(), ()):
            if target not in protected:
                protected.add(target)
                pending.append(target)
    return protected


def make_plan(root: Path, inventory_path: Path) -> dict[str, Any]:
    layout = RepoLayout(root)
    snapshot = layout.resolve(inventory_path).read_bytes()
    inventory = json.loads(snapshot)
    protected = _reference_closure(inventory)
    unresolved_references = (
        bool(inventory.get("reference_scan_limits", {}).get("truncated"))
        or any(
            record.get("status") == "UNKNOWN_REFERENCE"
            for record in inventory.get("records", [])
        )
        or any(
            ref.get("status") in {"UNKNOWN_REFERENCE", "unknown"}
            for ref in inventory.get("references", [])
        )
    )
    rows = []
    for record in inventory.get("records", []):
        relative = str(record["path"])
        if relative.startswith((".git/", "../")) or Path(relative).is_absolute():
            continue
        source_symlink = _has_symlink_component(layout, relative)
        try:
            source = layout.resolve(relative)
        except ValueError:
            continue
        status = record.get("status")
        referrers = record.get("referrers", [])
        active = bool(record.get("active_or_unknown_job"))
        pinned = (
            bool(record.get("pinned"))
            or active
            or bool(referrers)
            or relative in protected
        )
        candidate = (
            not unresolved_references
            and relative in REVIEW_ARCHIVE_PATHS
            and status == "AVAILABLE"
            and not pinned
        )
        source_hash = record.get("sha256")
        if candidate and not source_symlink and source.is_file():
            candidate = _sha256(source.read_bytes()) == source_hash
        else:
            candidate = False
        if candidate:
            reason = "unreferenced_historical_text_with_matching_hash"
        elif active:
            observed = (record.get("job") or {}).get("observed_status")
            reason = (
                "recoverable_job_requires_revalidation"
                if observed in {"recoverable", "failed"}
                else "active_or_unknown_job"
            )
        elif record.get("pinned"):
            reason = "pinned_evidence"
        elif referrers:
            reason = "referenced_by_current_source"
        elif relative in protected:
            reason = "reference_closure_or_unverified_status"
        elif unresolved_references:
            reason = "unresolved_reference_graph"
        elif source_symlink:
            reason = "source_symlink"
        elif relative not in REVIEW_ARCHIVE_PATHS:
            reason = "outside_review_scope"
        else:
            reason = "source_hash_or_file_state_changed"
        rows.append(
            {
                "path": relative,
                "action": "REVIEW_ARCHIVE_COPY" if candidate else "KEEP",
                "reason": reason,
                "source_sha256": source_hash,
                "size_bytes": record.get("size_bytes"),
                "referrers": referrers,
                "pinned_by": ["inventory_or_reference_closure"] if pinned else [],
                "active_job": record.get("job"),
                "destination": (
                    f"docs/archive/initial-reconstruction/{relative}" if candidate else None
                ),
                "restore_path": relative,
                "approval_required": True,
            }
        )
    identity = {
        "head_sha": _head(layout.root),
        "dirty_diff_sha256": _dirty_diff_sha256(layout.root),
        "inventory_sha256": _sha256(snapshot),
        "policy_version": POLICY_VERSION,
        "entries_sha256": _sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ),
    }
    return {
        "schema_version": 1,
        **identity,
        "plan_id": _sha256(json.dumps(identity, sort_keys=True).encode()),
        "apply_enabled": False,
        "unresolved_references": unresolved_references,
        "entries": rows,
    }


def validate_plan(root: Path, inventory_path: Path, plan: dict[str, Any]) -> list[str]:
    """Read-only preflight for a future approved archive transaction."""
    layout = RepoLayout(root)
    problems: list[str] = []
    snapshot = layout.resolve(inventory_path).read_bytes()
    if plan.get("head_sha") != _head(layout.root):
        problems.append("head_changed")
    current_dirty_diff = _dirty_diff_sha256(layout.root)
    if current_dirty_diff is not None and plan.get("dirty_diff_sha256") != current_dirty_diff:
        problems.append("dirty_diff_changed")
    if plan.get("inventory_sha256") != _sha256(snapshot):
        problems.append("inventory_snapshot_changed")
    if plan.get("policy_version") != POLICY_VERSION:
        problems.append("policy_version_changed")
    entries = plan.get("entries", [])
    expected_entries_sha = _sha256(
        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    if plan.get("entries_sha256") != expected_entries_sha:
        problems.append("plan_entries_changed")
    identity = {
        key: plan.get(key)
        for key in (
            "head_sha",
            "dirty_diff_sha256",
            "inventory_sha256",
            "policy_version",
            "entries_sha256",
        )
    }
    if plan.get("plan_id") != _sha256(json.dumps(identity, sort_keys=True).encode()):
        problems.append("plan_identity_changed")
    if plan.get("apply_enabled") is not False:
        problems.append("apply_disabled")
    if plan.get("unresolved_references"):
        problems.append("unresolved_references")
    from GemAgents.metabolic.jobs.lifecycle import ACTIVE, JobLedger

    ledger = JobLedger(layout.root)
    if ledger.corrupt_records() or any(
        record.get("status") in ACTIVE for _, record in ledger._records()
    ):
        problems.append("active_or_unknown_job")
    required_bytes = 0
    for row in entries:
        if row.get("action") != "REVIEW_ARCHIVE_COPY":
            continue
        try:
            source_name = str(row["path"])
            destination_value = str(row["destination"])
            if _has_symlink_component(layout, source_name):
                problems.append(f"source_symlink:{source_name}")
                continue
            source = layout.resolve(source_name)
            layout.writable(destination_value)
            destination = layout.resolve(destination_value)
        except (KeyError, ValueError):
            problems.append("invalid_candidate_path")
            continue
        if not source.is_file() or source.is_symlink():
            problems.append(f"source_missing_or_symlink:{row['path']}")
        elif _sha256(source.read_bytes()) != row.get("source_sha256"):
            problems.append(f"source_changed:{row['path']}")
        else:
            required_bytes += source.stat().st_size
        if destination.exists():
            problems.append(f"destination_conflict:{row['destination']}")
    if required_bytes > shutil.disk_usage(layout.root).free:
        problems.append("insufficient_disk_space")
    return problems


def review_jobs(root: Path) -> dict[str, Any]:
    """Build a read-only job reconciliation report without changing ledger files."""
    from GemAgents.metabolic.jobs.lifecycle import ACTIVE, TERMINAL, JobLedger

    ledger = JobLedger(root)
    rows: list[dict[str, Any]] = []
    for path, record in ledger._records():
        checkpoint = record.get("checkpoint")
        checkpoint_hash = (
            _sha256(
                json.dumps(
                    checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            if checkpoint is not None
            else None
        )
        status = record.get("status")
        worker_state = "terminal"
        if status in ACTIVE:
            try:
                worker_state = ledger.worker_state(str(record["job_id"]))
            except (OSError, TypeError, ValueError, KeyError):
                worker_state = "observation_error"
        recovery_identity_complete = bool(record.get("attempt_id")) and bool(
            record.get("content_key")
        )
        if status in TERMINAL:
            classification = "terminal"
        elif worker_state == "active":
            classification = (
                "active_worker"
                if recovery_identity_complete
                else "active_worker_missing_recovery_identity"
            )
        elif worker_state == "pid_reused":
            classification = "pid_reused_requires_review"
        elif worker_state == "not_registered":
            classification = "reservation_orphan_requires_review"
        elif worker_state == "exited":
            classification = (
                "exited_worker_missing_recovery_identity"
                if not recovery_identity_complete
                else "exited_worker_requires_reconciliation"
            )
        else:
            classification = "unknown_worker_state_requires_review"
        rows.append(
            {
                "path": str(path.relative_to(ledger.root)),
                "job_id": record.get("job_id"),
                "contract_id": record.get("contract_id"),
                "status": status,
                "phase": record.get("phase"),
                "pid": record.get("pid"),
                "process_start_identity": record.get("process_start_identity"),
                "attempt_id": record.get("attempt_id"),
                "content_key": record.get("content_key"),
                "recovery_identity_complete": recovery_identity_complete,
                "checkpoint_present": checkpoint is not None,
                "checkpoint_sha256": checkpoint_hash,
                "worker_state": worker_state,
                "classification": classification,
                "output": record.get("output"),
                "config_path": record.get("config_path"),
                "log": record.get("log"),
                "mutation": "none",
            }
        )
    corrupt = ledger.corrupt_records()
    for name in corrupt:
        rows.append(
            {
                "path": name,
                "job_id": None,
                "status": None,
                "worker_state": "unknown",
                "classification": "corrupt_primary_requires_review",
                "mutation": "none",
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row["classification"])
        counts[key] = counts.get(key, 0) + 1
    review_required = [
        row["path"] for row in rows if row["classification"] != "terminal"
    ]
    return {
        "schema_version": 1,
        "generated_unix": time.time(),
        "scope": "read_only_job_reconciliation",
        "ledger_root": str(ledger.root),
        "records": rows,
        "classification_counts": counts,
        "review_required": review_required,
        "safe_for_migration": not review_required,
        "mutations_applied": False,
        "limitations": [
            (
                "The report observes PID identity and ledger state only; it does not "
                "reconcile, cancel or recover jobs."
            ),
            (
                "A stale or exited worker remains protected until an explicit, "
                "separately approved reconciliation."
            ),
        ],
    }


def make_reconciliation_plan(root: Path, review: dict[str, Any]) -> dict[str, Any]:
    """Create a read-only, approval-gated plan for unresolved job records.

    The plan records only stable identities and required human actions.  It never
    infers a missing process identity, changes a ledger record, or authorizes a
    recovery/cancel operation.
    """
    if review.get("scope") != "read_only_job_reconciliation":
        raise ValueError("job review scope is not reconciliation-safe")
    if review.get("mutations_applied") is not False:
        raise ValueError("job review must declare mutations_applied=false")

    layout = RepoLayout(root)
    ledger = layout.jobs
    entries: list[dict[str, Any]] = []
    for row in review.get("records", []):
        if not isinstance(row, dict):
            raise ValueError("job review records must be objects")
        classification = str(row.get("classification", "unknown_worker_state_requires_review"))
        if classification == "terminal":
            continue
        relative = str(row.get("path", ""))
        try:
            primary = layout.resolve(ledger / relative)
        except ValueError as error:
            raise ValueError("job review record path escapes ledger") from error
        if not primary.is_file() or primary.is_symlink():
            raise ValueError(f"job review record is missing or symlinked: {relative}")
        if classification in {
            "active_worker_missing_recovery_identity",
            "exited_worker_missing_recovery_identity",
        }:
            required_actions = [
                "obtain authoritative process/termination evidence",
                "bind attempt_id and content_key before any recovery decision",
                "review output and manifest without rewriting the historical record",
            ]
            action = "REQUIRE_IDENTITY_EVIDENCE"
        elif classification == "exited_worker_requires_reconciliation":
            required_actions = [
                "verify process identity and termination evidence",
                "approve a separate reconciliation operation",
            ]
            action = "REQUIRE_APPROVED_RECONCILIATION"
        elif classification == "reservation_orphan_requires_review":
            required_actions = ["verify no worker was spawned", "approve orphan reconciliation"]
            action = "REQUIRE_RESERVATION_REVIEW"
        elif classification == "pid_reused_requires_review":
            required_actions = ["verify PID create-time identity", "approve manual disposition"]
            action = "REQUIRE_PID_REVIEW"
        else:
            required_actions = ["inspect the record and approve a manual disposition"]
            action = "REQUIRE_MANUAL_REVIEW"
        entries.append(
            {
                "path": relative,
                "job_id": row.get("job_id"),
                "classification": classification,
                "status": row.get("status"),
                "worker_state": row.get("worker_state"),
                "pid": row.get("pid"),
                "attempt_id": row.get("attempt_id"),
                "content_key_present": bool(row.get("content_key")),
                "checkpoint_sha256": row.get("checkpoint_sha256"),
                "record_sha256": _sha256(primary.read_bytes()),
                "action": action,
                "required_actions": required_actions,
                "approval_required": True,
                "mutation": "none",
            }
        )
    identity = {
        "review_sha256": _sha256(
            json.dumps(review, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ),
        "entries_sha256": _sha256(
            json.dumps(entries, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ),
        "policy_version": POLICY_VERSION,
    }
    return {
        "schema_version": 1,
        "scope": "read_only_job_reconciliation_plan",
        **identity,
        "plan_id": _sha256(json.dumps(identity, sort_keys=True).encode()),
        "apply_enabled": False,
        "approval_required": True,
        "safe_for_migration": False,
        "mutations_applied": False,
        "entries": entries,
        "limitations": [
            "This plan does not infer missing process identity or termination evidence.",
            (
                "No cancel, recover, reconcile, archive, migration or deletion operation "
                "is authorized."
            ),
            "Each entry requires separate human review before any ledger mutation.",
        ],
    }


def review_references(root: Path, inventory_path: Path) -> dict[str, Any]:
    """Summarize reference closure without exposing reference payloads or mutating files."""
    layout = RepoLayout(root)
    snapshot = layout.resolve(inventory_path).read_bytes()
    inventory = json.loads(snapshot)
    references = inventory.get("references", [])
    if not isinstance(references, list):
        raise ValueError("inventory references must be a list")

    statuses = Counter(
        str(reference.get("status") or "MISSING_STATUS") for reference in references
    )
    reasons = Counter(
        str(reference.get("reason") or "MISSING_REASON") for reference in references
    )
    kinds = Counter(
        str(reference.get("kind") or "MISSING_KIND") for reference in references
    )
    scan_limits = inventory.get("reference_scan_limits") or inventory.get("scan_limits") or {}
    scan_truncated = bool(scan_limits.get("truncated"))
    unresolved_statuses = sorted(
        status for status in statuses if status in {"UNKNOWN_REFERENCE", "unknown"}
    )
    non_present_statuses = sorted(status for status in statuses if status != "PRESENT")
    review_categories = [
        {"status": status, "count": statuses[status]}
        for status in non_present_statuses
    ]
    return {
        "schema_version": 1,
        "generated_unix": time.time(),
        "scope": "read_only_reference_closure",
        "head_sha": _head(layout.root),
        "inventory_sha256": _sha256(snapshot),
        "inventory_schema_version": inventory.get("schema_version"),
        "reference_count": len(references),
        "status_counts": dict(sorted(statuses.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "kind_counts": dict(sorted(kinds.items())),
        "scan_limits": scan_limits,
        "scan_truncated": scan_truncated,
        "unresolved_statuses": unresolved_statuses,
        "unresolved_reference_count": sum(statuses[s] for s in unresolved_statuses),
        "non_present_statuses": non_present_statuses,
        "review_categories": review_categories,
        "unresolved_references": bool(scan_truncated or unresolved_statuses),
        "safe_for_migration": not scan_truncated and not non_present_statuses,
        "mutations_applied": False,
        "limitations": [
            (
                "Counts are derived from the supplied inventory snapshot; reference "
                "targets are not re-read or reclassified."
            ),
            (
                "Generated, historical, external and policy-blocked references remain "
                "explicit review categories and are not treated as resolved."
            ),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["plan", "validate", "review-jobs", "reconcile-plan", "review-references"],
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--job-review", type=Path)
    args = parser.parse_args()
    if args.command in {"plan", "validate", "review-references"} and args.inventory is None:
        parser.error("--inventory is required")
    if args.command == "review-jobs":
        if args.output is None:
            parser.error("--output is required for review-jobs")
        layout = RepoLayout(args.root)
        output = layout.resolve(args.output)
        if not output.is_relative_to(layout.audit):
            raise ValueError("job review output must be inside artifacts/audit")
        output.parent.mkdir(parents=True, exist_ok=True)
        report = review_jobs(layout.root)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"output": str(output), "summary": report["classification_counts"]}))
        return
    if args.command == "reconcile-plan":
        if args.output is None:
            parser.error("--output is required for reconcile-plan")
        if args.job_review is None:
            parser.error("--job-review is required for reconcile-plan")
        layout = RepoLayout(args.root)
        review_path = layout.resolve(args.job_review)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        plan = make_reconciliation_plan(layout.root, review)
        output = layout.resolve(args.output)
        if not output.is_relative_to(layout.audit):
            raise ValueError("reconciliation plan output must be inside artifacts/audit")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(json.dumps({"output": str(output), "entries": len(plan["entries"])}))
        return
    if args.command == "review-references":
        if args.output is None:
            parser.error("--output is required for review-references")
        layout = RepoLayout(args.root)
        output = layout.resolve(args.output)
        if not output.is_relative_to(layout.audit):
            raise ValueError("reference review output must be inside artifacts/audit")
        output.parent.mkdir(parents=True, exist_ok=True)
        report = review_references(layout.root, args.inventory)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "reference_count": report["reference_count"],
                    "status_counts": report["status_counts"],
                    "safe_for_migration": report["safe_for_migration"],
                },
                ensure_ascii=False,
            )
        )
        return
    if args.command == "validate":
        if args.plan_file is None:
            parser.error("--plan-file is required for validate")
        layout = RepoLayout(args.root)
        plan_path = layout.resolve(args.plan_file)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        problems = validate_plan(layout.root, args.inventory, plan)
        report = {
            "schema_version": 1,
            "valid": not problems,
            "problems": problems,
            "plan_id": plan.get("plan_id"),
            "apply_enabled": plan.get("apply_enabled", False),
        }
        if args.output is not None:
            output = layout.resolve(args.output)
            if not output.is_relative_to(layout.audit):
                raise ValueError("validation output must be inside artifacts/audit")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        print(json.dumps(report, ensure_ascii=False))
        if problems:
            raise SystemExit(1)
        return
    if args.output is None:
        parser.error("--output is required for plan")
    plan = make_plan(args.root, args.inventory)
    layout = RepoLayout(args.root)
    output = layout.resolve(args.output)
    if not output.is_relative_to(layout.audit):
        raise ValueError("plan output must be inside artifacts/audit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
