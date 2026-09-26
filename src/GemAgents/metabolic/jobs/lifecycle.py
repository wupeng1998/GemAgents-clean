"""Persistent reconstruction job state and recovery primitives."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from GemAgents.layout import RepoLayout
from GemAgents.run_cache import atomic_json, stable_key

TERMINAL = frozenset({"completed", "completed_with_findings", "failed", "cancelled"})
SCHEMA_VERSION = 2
RECORD_TYPE = "metabolic_job"
ACTIVE = frozenset(
    {"reserved", "started", "running", "recoverable", "cancel_requested", "cancelling"}
)


def _is_job_id(value: object) -> bool:
    """Return whether ``value`` is a canonical persisted job identifier."""
    return isinstance(value, str) and len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def _acquire_ledger_lock(handle, platform_name: str) -> None:
    """Acquire the platform-specific exclusive ledger lock."""
    if platform_name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


TRANSITIONS = {
    "reserved": frozenset(
        {"started", "recoverable", "cancel_requested", "failed", "cancelled"}
    ),
    "started": frozenset(
        {
            "running", "recoverable", "cancel_requested", "completed",
            "completed_with_findings", "failed", "cancelled",
        }
    ),
    "running": frozenset(
        {
            "recoverable", "cancel_requested", "cancelling", "completed",
            "completed_with_findings", "failed", "cancelled",
        }
    ),
    "recoverable": frozenset({"running", "failed", "cancelled"}),
    "cancel_requested": frozenset({"cancelling", "cancelled", "failed"}),
    "cancelling": frozenset({"cancelled", "failed"}),
    "completed": frozenset(),
    "completed_with_findings": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def process_identity(pid: int) -> str | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        return f"{pid}:{process.create_time():.6f}"
    except (psutil.Error, OSError):
        return None


def process_group_identity(pid: int) -> str | None:
    """Return the owned process-group identity when the platform exposes it."""
    if os.name == "nt":
        return None
    try:
        return f"pgid:{os.getpgid(pid)}"
    except (OSError, ProcessLookupError):
        return None


class JobLedger:
    def __init__(self, workspace: Path) -> None:
        self.root = RepoLayout(workspace).jobs

    def _path(self, job_id: str) -> Path:
        if not _is_job_id(job_id):
            raise ValueError("invalid job ID")
        return self.root / f"{job_id}.json"

    def _records(self) -> list[tuple[Path, dict[str, Any]]]:
        """Read only primary records; sidecars and malformed records are excluded."""
        records = []
        if not self.root.is_dir():
            return records
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith((".contract.json", ".config.json")):
                continue
            if not _is_job_id(path.stem):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            # Older primary records remain readable; sidecars are excluded above.
            if record.get("record_type", RECORD_TYPE) != RECORD_TYPE:
                continue
            if record.get("job_id") != path.stem:
                continue
            if record.get("status") not in TRANSITIONS:
                continue
            records.append((path, record))
        return records

    def corrupt_records(self) -> list[str]:
        """Return malformed primary record names for an explicit reconciliation report."""
        corrupt = []
        if not self.root.is_dir():
            return corrupt
        for path in sorted(self.root.glob("*.json")):
            if path.name.endswith((".contract.json", ".config.json")):
                continue
            if not _is_job_id(path.stem):
                corrupt.append(path.name)
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                corrupt.append(path.name)
                continue
            if (
                not isinstance(record, dict)
                or record.get("job_id") != path.stem
                or record.get("status") not in TRANSITIONS
            ):
                corrupt.append(path.name)
        return corrupt

    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".ledger.lock"
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            _acquire_ledger_lock(handle, os.name)
        except (ImportError, OSError) as error:
            handle.close()
            raise RuntimeError("exclusive job ledger locking is unavailable") from error
        return handle

    def has_active_contract(self, contract_id: str) -> bool:
        if not self.root.is_dir():
            return False
        for _, record in self._records():
            if record.get("contract_id") == contract_id and record.get("status") in ACTIVE:
                return True
        return False

    def reserve(
        self,
        *,
        contract: dict[str, Any],
        output: Path,
        config_path: Path,
        content_key: str,
        log_path: Path,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._locked()
        try:
            for _, old in self._records():
                if (
                    old.get("contract_id") == contract.get("contract_id")
                    and old.get("status") in ACTIVE
                ):
                    raise RuntimeError("duplicate active reconstruction contract")
            job_id = job_id or uuid.uuid4().hex
            primary_path = self._path(job_id)
            contract_path = self.root / f"{job_id}.contract.json"
            if primary_path.exists() or contract_path.exists():
                raise RuntimeError("job ID already exists")
            now = time.time()
            record = {
                "record_type": RECORD_TYPE,
                "schema_version": SCHEMA_VERSION,
                "job_id": job_id,
                "contract_id": contract["contract_id"],
                "request_id": (
                    contract.get("authorization", {}).get("request_hash")
                    if isinstance(contract.get("authorization"), dict)
                    else None
                ),
                "config_path": str(config_path),
                "content_key": content_key,
                "pid": None,
                "process_start_identity": None,
                "process_group_identity": None,
                "adapter_identity": None,
                "attempt_id": uuid.uuid4().hex,
                "phase": "reserved",
                "checkpoint": None,
                "heartbeat_unix": now,
                "worker_heartbeat_unix": None,
                "heartbeat_timeout_seconds": 300,
                "submitted_unix": now,
                "output": str(output),
                "log": str(log_path),
                "status": "reserved",
                "cancel_requested": False,
                "permission": {
                    "side_effect": "reconstruction",
                    "contract_id": contract["contract_id"],
                    "request_id": (
                        contract.get("authorization", {}).get("request_hash")
                        if isinstance(contract.get("authorization"), dict)
                        else None
                    ),
                },
            }
            atomic_json(primary_path, record)
            try:
                atomic_json(contract_path, contract)
            except Exception:
                rollback_errors: list[Exception] = []
                for path in (primary_path, contract_path):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as rollback_error:
                        rollback_errors.append(rollback_error)
                if rollback_errors:
                    raise RuntimeError("reservation rollback failed") from rollback_errors[0]
                raise
            return record
        finally:
            lock.close()

    def register_process(self, job_id: str, pid: int) -> dict[str, Any]:
        lock = self._locked()
        try:
            record = self.load(job_id)
            status = record.get("status")
            if status not in {"reserved", "cancel_requested", "cancelling"} and not (
                status == "running" and record.get("resume_pending") is True
            ):
                raise ValueError(f"worker cannot be registered from {status}")
            record["pid"] = pid
            record["process_start_identity"] = process_identity(pid)
            record["process_group_identity"] = process_group_identity(pid)
            now = time.time()
            record["heartbeat_unix"] = now
            record["worker_heartbeat_unix"] = now
            if status == "reserved":
                record["status"] = "started"
                record["phase"] = "submitted"
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def abort_reservation(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Close a reservation when spawning or registration cannot complete.

        A reservation is written before a worker is spawned so duplicate
        submissions cannot race.  If the subsequent spawn/register step
        fails, leaving ``reserved`` active would block every future retry.
        This explicit transition preserves the failed attempt and makes the
        reservation safe to reconcile without guessing whether work started.
        """
        if not reason.strip():
            raise ValueError("reservation abort requires a reason")
        lock = self._locked()
        try:
            record = self.load(job_id)
            status = record.get("status")
            if status in TERMINAL:
                return record
            if status not in {"reserved", "started", "cancel_requested", "cancelling"}:
                raise ValueError(f"reservation cannot be aborted from {status}")
            cancelled = status in {"cancel_requested", "cancelling"}
            record.update(
                status="cancelled" if cancelled else "failed",
                phase="reservation_aborted",
                failure_reason=reason,
                heartbeat_unix=time.time(),
            )
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def create(
        self,
        *,
        contract: dict[str, Any],
        output: Path,
        config_path: Path,
        content_key: str,
        pid: int,
        log_path: Path,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        record = self.reserve(
            contract=contract,
            output=output,
            config_path=config_path,
            content_key=content_key,
            log_path=log_path,
            job_id=job_id,
        )
        return self.register_process(record["job_id"], pid)

    def load(self, job_id: str) -> dict[str, Any]:
        return json.loads(self._path(job_id).read_text(encoding="utf-8"))

    def update(
        self,
        job_id: str,
        *,
        expected_attempt_id: str | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        lock = self._locked()
        try:
            record = self.load(job_id)
            if (
                expected_attempt_id is not None
                and record.get("attempt_id") != expected_attempt_id
            ):
                raise ValueError("worker attempt identity changed")
            old_status = record.get("status")
            if "worker_heartbeat_unix" in updates and old_status not in {
                "started",
                "running",
                "cancel_requested",
                "cancelling",
            }:
                raise ValueError(f"worker heartbeat is not allowed from {old_status}")
            new_status = updates.get("status", old_status)
            if new_status != old_status and new_status not in TRANSITIONS.get(
                old_status, frozenset()
            ):
                raise ValueError(f"invalid job state transition: {old_status} -> {new_status}")
            record.update(updates)
            record["heartbeat_unix"] = time.time()
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def checkpoint(
        self,
        job_id: str,
        phase: str,
        checkpoint: dict[str, Any] | None = None,
        *,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"phase": phase, "checkpoint": checkpoint}
        current = self.load(job_id)
        if current.get("status") in {
            "started",
            "running",
            "cancel_requested",
            "cancelling",
        }:
            updates["worker_heartbeat_unix"] = time.time()
        return self.update(job_id, expected_attempt_id=attempt_id, **updates)

    def validate_worker_attempt(self, job_id: str, attempt_id: str | None) -> dict[str, Any]:
        """Return the current record only for the registered worker attempt."""
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("worker attempt identity is missing")
        record = self.load(job_id)
        if record.get("attempt_id") != attempt_id:
            raise ValueError("worker attempt identity changed")
        if record.get("status") not in {
            "started",
            "running",
            "cancel_requested",
            "cancelling",
        }:
            raise ValueError(f"worker attempt is not active: {record.get('status')}")
        return record

    def wait_for_worker_registration(
        self,
        job_id: str,
        attempt_id: str | None,
        *,
        pid: int,
        timeout_seconds: float = 5.0,
        poll_seconds: float = 0.01,
    ) -> dict[str, Any]:
        """Wait until the submitting parent binds this worker PID to its attempt.

        ``Popen`` returns before the parent can persist the child PID.  A fast
        worker must not consume a recovery checkpoint or publish a terminal
        state during that gap.  Attempt identity is checked on every read so a
        superseded or terminal job fails closed instead of waiting indefinitely.
        """
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("worker attempt identity is missing")
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("worker process identity is invalid")
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("worker registration wait must use positive timing bounds")
        deadline = time.monotonic() + timeout_seconds
        while True:
            record = self.load(job_id)
            if record.get("attempt_id") != attempt_id:
                raise ValueError("worker attempt identity changed")
            status = record.get("status")
            if record.get("pid") == pid:
                return self.validate_worker_attempt(job_id, attempt_id)
            waiting_state = status in {"reserved", "cancel_requested", "cancelling"} or (
                status == "running" and record.get("resume_pending") is True
            )
            if not waiting_state:
                raise ValueError(
                    f"worker registration is not allowed from {status}"
                )
            if time.monotonic() >= deadline:
                raise ValueError("worker registration was not observed before timeout")
            time.sleep(poll_seconds)

    def register_adapter(
        self,
        job_id: str,
        *,
        attempt_id: str,
        kind: str,
        identity: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind an externally managed adapter to the current worker attempt.

        The binding is deliberately small and identity-only: a killed worker may
        leave a container or other adapter alive, so cancellation can clean up
        only the object that this attempt explicitly registered.  No command
        string is accepted from the adapter, preventing a ledger record from
        becoming an arbitrary execution channel.
        """
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("adapter registration requires an attempt identity")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("adapter registration requires a kind")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("adapter registration requires an identity")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("adapter metadata must be an object")
        lock = self._locked()
        try:
            record = self.load(job_id)
            if record.get("attempt_id") != attempt_id:
                raise ValueError("worker attempt identity changed")
            if record.get("status") not in {
                "started",
                "running",
                "cancel_requested",
                "cancelling",
            }:
                raise ValueError(f"adapter cannot be registered from {record.get('status')}")
            current = record.get("adapter_identity")
            requested_metadata = dict(metadata or {})
            if current is not None:
                if not isinstance(current, dict):
                    raise ValueError("adapter identity record is invalid")
                if (
                    current.get("attempt_id") != attempt_id
                    or current.get("kind") != kind
                    or current.get("identity") != identity
                    or current.get("metadata", {}) != requested_metadata
                ):
                    raise ValueError("a different adapter is already registered")
                return record
            adapter = {
                "attempt_id": attempt_id,
                "kind": kind,
                "identity": identity,
                "metadata": requested_metadata,
                "registered_unix": time.time(),
            }
            record["adapter_identity"] = adapter
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def adapter_identity(self, job_id: str) -> dict[str, Any] | None:
        """Read the current adapter binding without changing the ledger."""
        record = self.load(job_id)
        adapter = record.get("adapter_identity")
        return dict(adapter) if isinstance(adapter, dict) else None

    def clear_adapter(
        self,
        job_id: str,
        *,
        attempt_id: str,
        identity: str,
    ) -> dict[str, Any]:
        """Clear an adapter binding only when attempt and identity both match."""
        lock = self._locked()
        try:
            record = self.load(job_id)
            adapter = record.get("adapter_identity")
            if adapter is None:
                return record
            if not isinstance(adapter, dict):
                raise ValueError("adapter identity record is invalid")
            if (
                adapter.get("attempt_id") != attempt_id
                or adapter.get("identity") != identity
            ):
                raise ValueError("adapter identity changed")
            record["adapter_identity"] = None
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def heartbeat(
        self,
        job_id: str,
        *,
        attempt_id: str,
        phase: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish a worker-owned heartbeat without changing job status."""
        if progress is not None and not isinstance(progress, dict):
            raise ValueError("worker heartbeat progress must be an object")
        updates: dict[str, Any] = {"worker_heartbeat_unix": time.time()}
        if phase is not None:
            updates["phase"] = phase
        if progress is not None:
            updates["progress"] = dict(progress)
        return self.update(
            job_id,
            expected_attempt_id=attempt_id,
            **updates,
        )

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        lock = self._locked()
        try:
            record = self.load(job_id)
            status = record.get("status")
            if status in TERMINAL:
                return record
            if status not in ACTIVE:
                raise ValueError(f"job cannot be cancelled from {status}")
            if status in {"cancel_requested", "cancelling"}:
                return record
            next_status = "cancelled" if status == "recoverable" else "cancel_requested"
            record.update(cancel_requested=True, status=next_status)
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def worker_state(self, job_id: str) -> str:
        record = self.load(job_id)
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return "not_registered"
        current = process_identity(pid)
        if current is None:
            return "exited"
        original = record.get("process_start_identity")
        if original is not None and current != original:
            return "pid_reused"
        return "active"

    def terminate_owned_worker(self, job_id: str, *, grace_seconds: float = 5.0) -> dict[str, Any]:
        """Request termination only for the worker process group we own.

        This is an explicit escalation path; cooperative cancellation remains the
        default.  The ledger is not marked terminal here because the worker may
        still need reconciliation and its phase checkpoint may be recoverable.
        """
        if not isinstance(grace_seconds, (int, float)) or not 0 <= grace_seconds <= 60:
            raise ValueError("termination grace must be between 0 and 60 seconds")
        record = self.load(job_id)
        if record.get("status") in TERMINAL:
            return {"delivery": "already_terminal", "worker_state": "terminal"}
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return {"delivery": "worker_not_registered", "worker_state": "not_registered"}
        expected_pid = record.get("process_start_identity")
        current_pid = process_identity(pid)
        if current_pid is None:
            return {"delivery": "worker_exited_requires_reconciliation", "worker_state": "exited"}
        if expected_pid is not None and current_pid != expected_pid:
            return {"delivery": "pid_reused_requires_review", "worker_state": "pid_reused"}
        if os.name == "nt":
            return {"delivery": "unsupported_platform", "worker_state": "active"}
        expected_group = record.get("process_group_identity")
        current_group = process_group_identity(pid)
        if not isinstance(expected_group, str) or expected_group != current_group:
            return {"delivery": "worker_group_identity_mismatch", "worker_state": "active"}
        if process_identity(pid) != expected_pid:
            return {"delivery": "pid_reused_requires_review", "worker_state": "pid_reused"}
        try:
            group_id = int(expected_group.removeprefix("pgid:"))
        except ValueError:
            return {"delivery": "worker_group_identity_invalid", "worker_state": "active"}
        if group_id <= 1 or group_id == os.getpgrp():
            return {"delivery": "unsafe_process_group", "worker_state": "active"}
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            return {"delivery": "worker_exited_requires_reconciliation", "worker_state": "exited"}
        deadline = time.monotonic() + float(grace_seconds)
        while time.monotonic() < deadline:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                return {"delivery": "terminated", "worker_state": "exited", "signal": "SIGTERM"}
            time.sleep(0.05)
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            return {"delivery": "terminated", "worker_state": "exited", "signal": "SIGTERM"}
        return {
            "delivery": "kill_escalated",
            "worker_state": "termination_pending",
            "signal": "SIGKILL",
        }

    def record_termination_evidence(
        self, job_id: str, *, delivery: str, reason: str = "explicit_termination"
    ) -> dict[str, Any]:
        """Persist evidence that the current worker attempt was terminated or observed gone."""
        if not delivery.strip():
            raise ValueError("termination evidence requires a delivery")
        lock = self._locked()
        try:
            record = self.load(job_id)
            evidence = {
                "kind": reason,
                "delivery": delivery,
                "pid": record.get("pid"),
                "process_start_identity": record.get("process_start_identity"),
                "process_group_identity": record.get("process_group_identity"),
                "recorded_unix": time.time(),
            }
            record["termination_evidence"] = evidence
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    def reconcile(self, job_id: str) -> dict[str, Any]:
        record = self.load(job_id)
        if record.get("status") in TERMINAL:
            return record
        if record.get("status") == "recoverable":
            # Recovery state already carries explicit termination evidence;
            # reconciling it again must be byte-for-byte idempotent.
            return record
        state = self.worker_state(job_id)
        if state == "active":
            return record
        if record.get("status") in {"reserved", "started"} and state == "not_registered":
            return self.update(
                job_id,
                status="failed",
                phase="reservation_orphaned",
                recovery_reason="worker_not_registered",
            )
        if record.get("status") in {"cancel_requested", "cancelling"}:
            status = "failed"
        else:
            status = "recoverable"
        self.update(
            job_id,
            status=status,
            phase="worker_exited_without_completion",
            recovery_reason=state,
        )
        return self.record_termination_evidence(
            job_id,
            delivery="worker_exited",
            reason="observed_worker_exit",
        )

    def reconcile_orphans(self) -> list[dict[str, Any]]:
        """Reconcile active primary records whose owned worker has exited.

        The candidate list is captured from the versioned primary-record
        reader, so contract/config sidecars and malformed JSON are never
        treated as jobs.  Each candidate is reconciled independently; records
        whose worker is still active are left byte-for-byte untouched and are
        not returned as changes.
        """
        candidates = [
            record["job_id"]
            for _, record in self._records()
            if record.get("status") in ACTIVE
        ]
        changed: list[dict[str, Any]] = []
        for job_id in candidates:
            before = self.load(job_id)
            after = self.reconcile(job_id)
            if after != before:
                changed.append(after)
        return changed

    def load_recovery_artifacts(
        self, job_id: str, *, content_key: str
    ) -> dict[str, Any]:
        """Load a recoverable checkpoint's verified artifact bundle.

        Recovery callers must consume the same manifest and artifact hashes that
        ``recover`` validates.  Returning paths only after re-validating them
        prevents a later stage loader from accidentally trusting a changed,
        missing or symlinked file.  This method is read-only; it does not rotate
        the attempt or change the ledger state.
        """
        record = self.load(job_id)
        if record.get("content_key") != content_key:
            raise ValueError("job content identity changed; refusing recovery")
        status = record.get("status")
        if status != "recoverable" and not (
            status == "running" and record.get("resume_pending") is True
        ):
            raise ValueError(
                f"recovery artifacts require a recoverable or pending-resume job, got {status}"
            )
        if record.get("process_start_identity") and not isinstance(
            record.get("termination_evidence"), dict
        ):
            raise ValueError("recoverable worker has no termination evidence; refusing recovery")
        checkpoint = record.get("checkpoint")
        if not isinstance(checkpoint, dict) or not checkpoint:
            raise ValueError("recoverable job has no checkpoint; refusing recovery")
        expected_attempt = record.get("attempt_id")
        if status == "running":
            expected_attempt = record.get("recovery_source_attempt_id")
        if checkpoint.get("attempt_id") != expected_attempt:
            raise ValueError("checkpoint attempt identity changed; refusing recovery")
        checkpoint_content = checkpoint.get("content_key")
        if checkpoint_content is not None and checkpoint_content != content_key:
            raise ValueError("checkpoint content identity changed; refusing recovery")
        manifest_sha256 = checkpoint.get("manifest_sha256")
        artifact_hashes = checkpoint.get("artifact_hashes")
        if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
            raise ValueError("checkpoint manifest hash is invalid; refusing recovery")
        if not isinstance(artifact_hashes, dict):
            raise ValueError("checkpoint artifact hashes are invalid; refusing recovery")

        workspace = self.root.parent.parent.resolve()
        output_value = record.get("output")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("recoverable job output is missing; refusing recovery")
        output = Path(output_value)
        output = ((workspace / output).resolve() if not output.is_absolute() else output.resolve())
        try:
            output.relative_to(workspace)
        except ValueError as error:
            raise ValueError("checkpoint output escapes workspace; refusing recovery") from error
        from GemAgents.provenance import restricted_path

        manifest_path = output / "manifest.json"
        if restricted_path(manifest_path) or not manifest_path.is_file():
            raise ValueError("checkpoint manifest is missing or restricted; refusing recovery")
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_sha256:
            raise ValueError("checkpoint manifest changed or is missing; refusing recovery")

        artifact_paths: dict[str, str] = {}
        for relative, expected in artifact_hashes.items():
            relative_path = Path(relative) if isinstance(relative, str) else None
            if (
                relative_path is None
                or not isinstance(expected, str)
                or len(expected) != 64
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or not relative
            ):
                raise ValueError("checkpoint artifact path is invalid; refusing recovery")
            artifact = output / relative_path
            if artifact.is_symlink() or not artifact.is_file():
                raise ValueError(
                    "checkpoint artifact is missing or symlinked; refusing recovery"
                )
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                raise ValueError("checkpoint artifact changed; refusing recovery")
            artifact_paths[relative_path.as_posix()] = str(artifact)
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("checkpoint manifest is not valid JSON; refusing recovery") from error
        if not isinstance(manifest_payload, dict):
            raise ValueError("checkpoint manifest must be an object; refusing recovery")
        return {
            "job_id": job_id,
            "attempt_id": record.get("attempt_id"),
            "source_attempt_id": expected_attempt,
            "phase": checkpoint.get("phase", record.get("phase")),
            "manifest": manifest_payload,
            "manifest_path": str(manifest_path),
            "artifact_paths": artifact_paths,
            "artifact_hashes": dict(artifact_hashes),
        }

    def recover(self, job_id: str, *, content_key: str, pid: int | None = None) -> dict[str, Any]:
        lock = self._locked()
        try:
            record = self.load(job_id)
            if record.get("content_key") != content_key:
                raise ValueError("job content identity changed; refusing recovery")
            if record.get("status") in TERMINAL:
                return record
            if record.get("adapter_identity") is not None:
                raise ValueError(
                    "recoverable job still owns an adapter; clean it up before recovery"
                )
            if record.get("status") == "recoverable":
                if record.get("process_start_identity") and not isinstance(
                    record.get("termination_evidence"), dict
                ):
                    raise ValueError(
                        "recoverable worker has no termination evidence; refusing recovery"
                    )
                checkpoint = record.get("checkpoint")
                if not isinstance(checkpoint, dict) or not checkpoint:
                    raise ValueError("recoverable job has no checkpoint; refusing recovery")
                checkpoint_attempt = checkpoint.get("attempt_id")
                expected_checkpoint_attempt = (
                    record.get("recovery_source_attempt_id")
                    if record.get("resume_pending") is True
                    else record.get("attempt_id")
                )
                if not isinstance(checkpoint_attempt, str) or not checkpoint_attempt:
                    raise ValueError(
                        "checkpoint attempt identity is missing; refusing recovery"
                    )
                if checkpoint_attempt != expected_checkpoint_attempt:
                    raise ValueError(
                        "checkpoint attempt identity changed; refusing recovery"
                    )
                checkpoint_content = checkpoint.get("content_key")
                if checkpoint_content is not None and checkpoint_content != content_key:
                    raise ValueError("checkpoint content identity changed; refusing recovery")
                manifest_sha256 = checkpoint.get("manifest_sha256")
                if manifest_sha256 is not None:
                    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
                        raise ValueError("checkpoint manifest hash is invalid; refusing recovery")
                    output = Path(str(record.get("output", "")))
                    workspace = self.root.parent.parent
                    output = (
                        (workspace / output).resolve()
                        if not output.is_absolute()
                        else output.resolve()
                    )
                    try:
                        output.relative_to(workspace.resolve())
                    except ValueError as error:
                        raise ValueError(
                            "checkpoint output escapes workspace; refusing recovery"
                        ) from error
                    manifest_path = output / "manifest.json"
                    from GemAgents.provenance import restricted_path

                    if restricted_path(manifest_path):
                        raise ValueError(
                            "checkpoint output is restricted; refusing recovery"
                        )
                    if not manifest_path.is_file() or hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest() != manifest_sha256:
                        raise ValueError(
                            "checkpoint manifest changed or is missing; refusing recovery"
                        )
                    artifact_hashes = checkpoint.get("artifact_hashes")
                    if artifact_hashes is not None:
                        if not isinstance(artifact_hashes, dict):
                            raise ValueError("checkpoint artifact hashes are invalid")
                        for relative, expected in artifact_hashes.items():
                            if (
                                not isinstance(relative, str)
                                or not isinstance(expected, str)
                                or Path(relative).is_absolute()
                                or ".." in Path(relative).parts
                            ):
                                raise ValueError(
                                    "checkpoint artifact path is invalid; refusing recovery"
                                )
                            artifact = output / relative
                            if artifact.is_symlink() or not artifact.is_file():
                                raise ValueError(
                                    "checkpoint artifact is missing or symlinked; refusing recovery"
                                )
                            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                                raise ValueError(
                                    "checkpoint artifact changed; refusing recovery"
                                )
            old_pid = record.get("pid")
            current_identity = process_identity(int(old_pid)) if old_pid else None
            original_identity = record.get("process_start_identity")
            if current_identity is not None and (
                original_identity is None or current_identity == original_identity
            ):
                raise RuntimeError("worker is still active; recovery would duplicate work")
            if pid is not None:
                record["pid"] = pid
                record["process_start_identity"] = process_identity(pid)
                record["process_group_identity"] = process_group_identity(pid)
            previous_attempt = record.get("attempt_id")
            record["attempt_id"] = uuid.uuid4().hex
            if record.get("status") == "recoverable":
                record["resume_pending"] = True
                record["recovery_source_attempt_id"] = previous_attempt
            record["status"] = "running"
            record["cancel_requested"] = False
            now = time.time()
            record["heartbeat_unix"] = now
            record["worker_heartbeat_unix"] = now
            atomic_json(self._path(job_id), record)
            return record
        finally:
            lock.close()

    @staticmethod
    def content_key(contract: dict[str, Any], config_bytes: bytes) -> str:
        return stable_key(
            {
                "contract": contract,
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            }
        )
