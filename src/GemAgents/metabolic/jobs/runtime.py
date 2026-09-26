"""Deterministic job observation and cancellation operations.

These operations stay separate from the legacy reconstruction pipeline so
read-only status checks and ledger updates do not import optional scientific
dependencies.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout
from GemAgents.metabolic.jobs.lifecycle import JobLedger


def metabolic_status(runner: Any, job_id: str) -> str:
    """Read a reconstruction snapshot without mutating its ledger record."""
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ToolError("Invalid metabolic job ID")
    path = RepoLayout(runner.workspace).jobs / f"{job_id}.json"
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ToolError(f"Unable to read metabolic job: {job_id}") from error
    if not isinstance(job, dict):
        raise ToolError(f"Invalid metabolic job record: {job_id}")

    observed = dict(job)
    observed.setdefault("job_id", job_id)
    manifest = runner._resolve(job["output"]) / "manifest.json"
    if manifest.is_file():
        try:
            result = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ToolError(f"Invalid metabolic result manifest: {job_id}") from error
        if isinstance(result, dict):
            observed["result"] = result
            observed["observed_output_status"] = result.get("status")

    if observed["status"] in {"started", "running", "cancel_requested", "cancelling"}:
        _observe_worker(observed)
    return json.dumps(observed, ensure_ascii=False)


def _observe_worker(observed: dict[str, object]) -> None:
    """Add a worker observation without changing the persisted record."""
    try:
        import psutil

        pid = observed.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            observed["observed_worker_state"] = "starting_or_worker_unavailable"
            observed["status"] = "starting_or_worker_unavailable"
            return
        process = psutil.Process(pid)
        current_identity = f"{pid}:{process.create_time():.6f}"
        original_identity = observed.get("process_start_identity")
        if not process.is_running() or (
            original_identity and current_identity != original_identity
        ) or (
            not original_identity
            and abs(process.create_time() - float(observed.get("submitted_unix", 0))) > 10
        ):
            observed["observed_worker_state"] = "worker_exited_without_completion"
            observed["status"] = "worker_exited_without_completion"
        else:
            heartbeat = observed.get("worker_heartbeat_unix")
            if heartbeat is None:
                heartbeat = observed.get("heartbeat_unix")
            timeout = observed.get("heartbeat_timeout_seconds", 300)
            try:
                heartbeat_age = time.time() - float(heartbeat)
                heartbeat_timeout = float(timeout)
            except (TypeError, ValueError):
                heartbeat_age = None
                heartbeat_timeout = 300.0
            if heartbeat_age is not None:
                observed["observed_heartbeat_age_seconds"] = max(0.0, heartbeat_age)
            if heartbeat_age is not None and heartbeat_age > heartbeat_timeout:
                observed["observed_worker_state"] = "worker_stalled"
                observed["status"] = "worker_stalled"
            else:
                observed["observed_worker_state"] = "running"
    except ImportError:
        observed["observed_worker_state"] = "starting_or_worker_unavailable"
        observed["status"] = "starting_or_worker_unavailable"
    except psutil.Error:
        observed["observed_worker_state"] = "worker_exited_without_completion"
        observed["status"] = "worker_exited_without_completion"
    except (OSError, ValueError, TypeError):
        observed["observed_worker_state"] = "worker_exited_without_completion"
        observed["status"] = "worker_exited_without_completion"


def metabolic_cancel(
    runner: Any,
    job_id: str,
    *,
    terminate: bool = False,
    grace_seconds: float = 5.0,
) -> str:
    """Request cancellation, optionally escalating to an owned worker group."""
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ToolError("Invalid metabolic job ID")
    try:
        ledger = JobLedger(runner.workspace)
        job = ledger.request_cancel(job_id)
    except (OSError, ValueError, KeyError) as error:
        raise ToolError(str(error)) from error
    # Cancellation is cooperative: the persisted state records only the
    # request.  Observe the worker after that write so callers can distinguish
    # a request waiting for a phase boundary from an exited or reused worker.
    delivery = "already_terminal"
    worker_state = "terminal"
    if job.get("status") not in {
        "completed",
        "completed_with_findings",
        "failed",
        "cancelled",
    }:
        try:
            worker_state = ledger.worker_state(job_id)
        except (OSError, ValueError, KeyError, TypeError):
            worker_state = "observation_error"
        delivery = {
            "active": "pending_phase_boundary",
            "exited": "worker_exited_requires_reconciliation",
            "pid_reused": "pid_reused_requires_review",
            "not_registered": "worker_not_registered",
        }.get(worker_state, "worker_observation_unavailable")
    job = dict(job)
    job["observed_worker_state"] = worker_state
    job["cancel_delivery"] = delivery
    job["mutation"] = "cancel_request_only"
    if terminate and delivery == "pending_phase_boundary":
        try:
            termination = ledger.terminate_owned_worker(
                job_id, grace_seconds=grace_seconds
            )
        except (OSError, ValueError, KeyError) as error:
            termination = {"delivery": "termination_error", "error": str(error)}
        if termination.get("delivery") in {"terminated", "kill_escalated"}:
            adapter_cleanup = _cleanup_owned_adapter(ledger, job_id)
            termination = {**termination, "adapter_cleanup": adapter_cleanup}
            try:
                ledger.record_termination_evidence(
                    job_id,
                    delivery=str(termination["delivery"]),
                )
            except (OSError, ValueError, KeyError) as error:
                termination = {
                    **termination,
                    "evidence_error": str(error),
                }
        job["termination"] = termination
        job["cancel_delivery"] = termination.get("delivery", "termination_unknown")
        job["mutation"] = "cancel_request_and_worker_termination"
    return json.dumps(job, ensure_ascii=False)


def _cleanup_owned_adapter(ledger: JobLedger, job_id: str) -> dict[str, object]:
    """Best-effort cleanup of the adapter explicitly owned by this attempt."""
    adapter = ledger.adapter_identity(job_id)
    if adapter is None:
        return {"delivery": "no_registered_adapter"}
    try:
        from GemAgents.metabolic.pgap import cleanup_pgap_adapter

        cleanup_payload = dict(adapter)
        metadata = cleanup_payload.get("metadata")
        if isinstance(metadata, dict):
            cleanup_payload.update(
                {key: value for key, value in metadata.items() if key not in cleanup_payload}
            )
        result = cleanup_pgap_adapter(cleanup_payload)
        ledger.clear_adapter(
            job_id,
            attempt_id=str(adapter.get("attempt_id", "")),
            identity=str(adapter.get("identity", "")),
        )
        return result
    except (OSError, ToolError, ValueError, KeyError) as error:
        return {
            "delivery": "adapter_cleanup_failed",
            "kind": adapter.get("kind"),
            "identity": adapter.get("identity"),
            "error": str(error),
        }
