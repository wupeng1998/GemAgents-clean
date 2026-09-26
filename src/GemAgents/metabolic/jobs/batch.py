"""Background submission for the auditable BiGG directory batch runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout


def _regular_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ToolError(f"{label} must be a regular workspace directory")
    return path


def metabolic_batch_start(
    runner: Any,
    input_directory: str,
    output_directory: str,
    mapping: str,
    biomass_library: str,
    reaction_library: str,
    hmm_directory: str,
) -> str:
    """Submit the repository's resumable BiGG batch script as a child job."""
    input_path = _regular_directory(runner._resolve(input_directory), "batch input")
    output_path = runner._resolve(output_directory)
    mapping_path = runner._resolve(mapping)
    biomass_path = _regular_directory(runner._resolve(biomass_library), "biomass library")
    reaction_path = _regular_directory(runner._resolve(reaction_library), "reaction library")
    hmm_path = _regular_directory(runner._resolve(hmm_directory), "HMM directory")
    if mapping_path.is_symlink() or not mapping_path.is_file():
        raise ToolError("batch mapping must be a regular workspace file")
    script = runner.workspace / "scripts" / "run_bigg_gemagents_batch.py"
    if not script.is_file():
        raise ToolError("batch runner script is missing: scripts/run_bigg_gemagents_batch.py")

    jobs = RepoLayout(runner.workspace).jobs
    jobs.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    log_path = jobs / f"batch-{job_id}.log"
    record_path = jobs / f"batch-{job_id}.json"
    command = [
        sys.executable,
        str(script),
        "--workspace",
        str(runner.workspace),
        "--mapping",
        str(mapping_path),
        "--biomass-library",
        str(biomass_path),
        "--reaction-library",
        str(reaction_path),
        "--hmm-directory",
        str(hmm_path),
        "--output-directory",
        str(output_path),
    ]
    env = runner._command_env()
    try:
        with log_path.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=runner.workspace,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
    except OSError as error:
        log_path.unlink(missing_ok=True)
        raise ToolError(f"batch worker could not start: {error}") from error

    payload = {
        "job_id": job_id,
        "status": "submitted",
        "pid": process.pid,
        "input_directory": str(input_path),
        "output_directory": str(output_path),
        "mapping": str(mapping_path),
        "log": str(log_path),
        "command": command,
    }
    try:
        record_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as error:
        process.terminate()
        raise ToolError(f"batch worker record could not be written: {error}") from error
    return json.dumps(payload, ensure_ascii=False)


def metabolic_batch_status(runner: Any, job_id: str) -> str:
    if not isinstance(job_id, str) or len(job_id) != 32 or not all(
        char in "0123456789abcdef" for char in job_id
    ):
        raise ToolError("Invalid batch job ID")
    record_path = RepoLayout(runner.workspace).jobs / f"batch-{job_id}.json"
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError("Batch job does not exist") from error
    if payload.get("status") in {"completed", "failed", "cancelled"}:
        return json.dumps(payload, ensure_ascii=False)
    status_path = Path(str(payload["output_directory"])) / "batch-status.json"
    summary = None
    if status_path.is_file():
        try:
            summary = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
    if not isinstance(summary, dict):
        summary = None

    try:
        pid = int(payload["pid"])
        if pid <= 0:
            raise ValueError("worker pid must be positive")
        os.kill(pid, 0)
        worker_alive = True
    except PermissionError:
        worker_alive = True
    except (ProcessLookupError, ValueError, TypeError, OSError):
        worker_alive = False

    if worker_alive:
        payload["status"] = "running"
        payload.pop("failure_reason", None)
    elif isinstance(summary, dict) and summary.get("status") == "completed":
        # A completed batch may contain failed genomes; preserve those per-build
        # findings in the summary while marking the batch execution terminal.
        payload["status"] = "completed"
        payload.pop("failure_reason", None)
    else:
        payload["status"] = "failed"
        payload["failure_reason"] = "worker exited before publishing a completed batch summary"

    if summary is not None:
        payload["summary"] = summary
    record_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["metabolic_batch_start", "metabolic_batch_status"]
