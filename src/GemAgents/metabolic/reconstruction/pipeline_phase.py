"""Phase-boundary checkpointing and cooperative cancellation for reconstruction."""

from __future__ import annotations

from pathlib import Path


def _phase_artifact_hashes(out: Path, hash_path) -> dict[str, str]:
    """Hash phase outputs without including the self-referential manifest."""
    artifacts: dict[str, str] = {}
    for path in sorted(out.rglob("*")):
        if path.name == "manifest.json" or not path.is_file() or path.is_symlink():
            continue
        artifacts[path.relative_to(out).as_posix()] = hash_path(path)
    return artifacts


def advance_phase(
    name: str,
    *,
    manifest: dict,
    out: Path,
    worker_ledger,
    worker_job_id: str | None,
    worker_attempt_id: str | None,
    cancelled_error,
    write_json,
    hash_path,
    now,
    emit,
) -> None:
    """Publish a phase checkpoint, honoring cancellation at the phase boundary."""
    artifact_hashes = _phase_artifact_hashes(out, hash_path)
    if worker_ledger is not None:
        current = worker_ledger.load(worker_job_id)
        if current.get("cancel_requested") and name not in {"cancelled", "failed"}:
            worker_ledger.update(worker_job_id, status="cancelling", phase=name)
            manifest.update(
                status="cancelled",
                execution_status="cancelled",
                phase=name,
                failed_phase=name,
                cancel_reason="requested_at_phase_boundary",
                phase_artifact_hashes=artifact_hashes,
            )
            manifest["elapsed_seconds"] = now() - manifest["started_unix"]
            write_json(out / "manifest.json", manifest)
            worker_ledger.checkpoint(
                worker_job_id,
                name,
                {
                    "phase": name,
                    "manifest_sha256": hash_path(out / "manifest.json"),
                    "artifact_hashes": artifact_hashes,
                    "progress": {"phase": name, "artifact_count": len(artifact_hashes)},
                    "cancel_requested": True,
                    "content_key": current.get("content_key"),
                    "attempt_id": worker_attempt_id,
                },
                attempt_id=worker_attempt_id,
            )
            if current.get("resume_pending") is True:
                worker_ledger.update(
                    worker_job_id,
                    resume_pending=False,
                    recovery_source_attempt_id=None,
                )
            worker_ledger.update(worker_job_id, status="cancelled", phase="cancelled")
            raise cancelled_error("reconstruction cancelled at phase boundary")
    manifest["phase"] = name
    manifest["phase_artifact_hashes"] = artifact_hashes
    manifest["elapsed_seconds"] = now() - manifest["started_unix"]
    write_json(out / "manifest.json", manifest)
    if worker_ledger is not None:
        worker_ledger.checkpoint(
            worker_job_id,
            name,
            {
                "phase": name,
                "manifest_sha256": hash_path(out / "manifest.json"),
                "artifact_hashes": artifact_hashes,
                "progress": {"phase": name, "artifact_count": len(artifact_hashes)},
                "content_key": current.get("content_key"),
                "attempt_id": worker_attempt_id,
            },
            attempt_id=worker_attempt_id,
        )
        if current.get("resume_pending") is True:
            worker_ledger.update(
                worker_job_id,
                resume_pending=False,
                recovery_source_attempt_id=None,
            )
    emit(f"[reconstruction] {name}")
