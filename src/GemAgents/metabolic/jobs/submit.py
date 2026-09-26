"""Background reconstruction job submission orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from GemAgents.contracts import apply_reconstruction_defaults
from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout
from GemAgents.metabolic.contracts import load_configuration
from GemAgents.metabolic.io import metabolic_json
from GemAgents.metabolic.jobs.lifecycle import JobLedger


def metabolic_start(
    runner: Any,
    config_path: str,
    *,
    contract: dict[str, object] | None = None,
    validate_options_fn: Callable[[dict], None],
    compile_contract_fn: Callable[..., Any] | None = None,
    ledger_factory: Callable[[Path], JobLedger] = JobLedger,
    write_json_fn: Callable[[Path, object], None] = metabolic_json,
    popen_fn: Callable[..., Any] | None = None,
    uuid_fn: Callable[[], Any] = uuid.uuid4,
    python_executable: str = sys.executable,
    platform_name: str = os.name,
) -> str:
    """Run long reconstruction work in a hidden child, outside tool timeout."""
    path = runner._resolve(config_path)
    config = apply_reconstruction_defaults(load_configuration(path), runner.workspace)
    validate_options_fn(config)
    if contract is None:
        if compile_contract_fn is None:
            raise ToolError("compile_contract callback is required")
        contract = compile_contract_fn(
            config, runner.workspace, config_path=path
        ).as_dict()
    for name in (
        "input",
        "annotation_gbk",
        "pgap_output",
        "pgap_script",
        "pgap_config",
        "universe",
        "reaction_library",
        "biomass_library",
        "medium_file",
        "modelseed_reactions",
        "hmm_dir",
        "clean_predictions",
        "clean_runtime",
        "clean_python",
        "reference_support_path",
        "output",
    ):
        if config.get(name):
            value = Path(str(config[name])).expanduser()
            if name == "clean_predictions" and value.is_absolute():
                config[name] = str(value.resolve())
            elif name == "clean_predictions":
                # Pipeline context resolves workspace-relative CLEAN paths;
                # keeping the spelling here preserves the prepared contract ID.
                continue
            elif name in {"clean_runtime", "clean_python"}:
                config[name] = str(value.resolve())
            else:
                config[name] = str(runner._resolve(str(config[name])))
    layout = RepoLayout(runner.workspace)
    jobs = layout.jobs
    jobs.mkdir(parents=True, exist_ok=True)
    ledger = ledger_factory(runner.workspace)
    if ledger.has_active_contract(str(contract["contract_id"])):
        raise ToolError("duplicate active reconstruction contract")
    job_id = uuid_fn().hex
    output = Path(config.setdefault("output", str(layout.runs / job_id)))
    if output.exists() and any(output.iterdir()):
        raise ToolError("Output must be a new or empty run directory")
    submitted = jobs / f"{job_id}.config.json"
    if submitted.exists():
        raise ToolError("job submission config already exists")
    log_path = jobs / f"{job_id}.log"
    try:
        write_json_fn(submitted, config)
        content_key = ledger.content_key(contract, submitted.read_bytes())
        reserved = ledger.reserve(
            contract=contract,
            output=output,
            config_path=submitted,
            content_key=content_key,
            log_path=log_path,
            job_id=job_id,
        )
    except Exception:
        try:
            submitted.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise ToolError(
                "job reservation failed and submitted config cleanup failed"
            ) from cleanup_error
        raise
    try:
        command_env = runner._command_env()
        command_env["GEMAGENTS_JOB_ID"] = reserved["job_id"]
        command_env["GEMAGENTS_JOB_ATTEMPT_ID"] = reserved["attempt_id"]
    except Exception:
        ledger.abort_reservation(reserved["job_id"], reason="worker setup failed")
        raise
    creationflags = subprocess.CREATE_NO_WINDOW if platform_name == "nt" else 0
    if platform_name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    spawn_options = {"creationflags": creationflags}
    if platform_name != "nt":
        spawn_options["start_new_session"] = True
    popen_fn = subprocess.Popen if popen_fn is None else popen_fn
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = popen_fn(
                [
                    python_executable,
                    "-m",
                    "GemAgents",
                    "--workspace",
                    str(runner.workspace),
                    "--reconstruct-config",
                    str(submitted),
                ],
                cwd=runner.workspace,
                env=command_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **spawn_options,
            )
    except Exception:
        ledger.abort_reservation(reserved["job_id"], reason="worker spawn failed")
        raise
    try:
        job = ledger.register_process(reserved["job_id"], process.pid)
    except Exception as error:
        # Registration is the ownership boundary. Stop the child before
        # aborting the reservation so no worker can run without a ledger row.
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        try:
            ledger.abort_reservation(
                reserved["job_id"],
                reason=f"worker registration failed: {type(error).__name__}",
            )
        except Exception as abort_error:
            raise ToolError(
                "worker registration failed and reservation cleanup failed"
            ) from abort_error
        raise ToolError("worker registration failed; reservation aborted") from error
    return json.dumps(job)


def metabolic_resume(
    runner: Any,
    job_id: str,
    *,
    ledger_factory: Callable[[Path], JobLedger] = JobLedger,
    popen_fn: Callable[..., Any] | None = None,
    python_executable: str = sys.executable,
    platform_name: str = os.name,
) -> str:
    """Start a new worker from a verified reaction-mapping checkpoint.

    The existing job record and contract sidecar remain authoritative.  The
    source attempt is validated before rotation; a new process is registered
    only after the ledger records the pending resume identity.
    """
    if not isinstance(job_id, str) or not job_id.isalnum() or len(job_id) != 32:
        raise ToolError("Invalid metabolic job ID")
    ledger = ledger_factory(runner.workspace)
    record = ledger.load(job_id)
    if record.get("status") != "recoverable":
        raise ToolError(f"Job is not recoverable: {record.get('status')}")
    if record.get("adapter_identity") is not None:
        raise ToolError(
            "recoverable job still owns an adapter; terminate or reconcile "
            "the adapter before recovery"
        )
    config_value = record.get("config_path")
    if not isinstance(config_value, str) or not config_value:
        raise ToolError("Recoverable job has no configuration path")
    config_path = runner._resolve(config_value)
    contract_path = ledger.root / f"{job_id}.contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        config_bytes = config_path.read_bytes()
    except (OSError, ValueError, TypeError) as error:
        raise ToolError("Recoverable job contract or configuration is unreadable") from error
    if not isinstance(contract, dict):
        raise ToolError("Recoverable job contract is invalid")
    content_key = ledger.content_key(contract, config_bytes)
    if content_key != record.get("content_key"):
        raise ToolError("job content identity changed; refusing recovery")
    try:
        bundle = ledger.load_recovery_artifacts(job_id, content_key=content_key)
        if bundle.get("phase") != "reaction_mapping":
            raise ValueError(
                "recovery checkpoint phase is not restartable by the current worker adapter"
            )
        # The ledger content key protects the submitted contract/config bytes,
        # but the source sequence can change independently of those files.  A
        # worker-owned checkpoint carries the input hash from its manifest;
        # verify that hash before rotating the attempt or spawning a worker.
        checkpoint_manifest = bundle.get("manifest")
        expected_input_hash = (
            checkpoint_manifest.get("input_sha256")
            if isinstance(checkpoint_manifest, dict)
            else None
        )
        if expected_input_hash is not None and isinstance(contract.get("input_path"), str):
            input_value = contract["input_path"]
            input_path = Path(input_value)
            if not input_path.is_absolute():
                input_path = runner.workspace / input_path
            try:
                actual_input_hash = hashlib.sha256(input_path.resolve().read_bytes()).hexdigest()
            except (OSError, ValueError) as error:
                raise ValueError(
                    "recovery checkpoint input identity cannot be read"
                ) from error
            if actual_input_hash != expected_input_hash:
                raise ValueError("job input identity changed; refusing recovery")
        recovered = ledger.recover(job_id, content_key=content_key)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        raise ToolError(str(error)) from error

    log_value = recovered.get("log")
    log_path = runner._resolve(str(log_value)) if isinstance(log_value, str) else (
        ledger.root / f"{job_id}.log"
    )
    command_env = runner._command_env()
    command_env.update(
        {
            "GEMAGENTS_JOB_ID": job_id,
            "GEMAGENTS_JOB_ATTEMPT_ID": str(recovered["attempt_id"]),
            "GEMAGENTS_RESUME_JOB_ID": job_id,
        }
    )
    creationflags = subprocess.CREATE_NO_WINDOW if platform_name == "nt" else 0
    if platform_name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    spawn_options = {"creationflags": creationflags}
    if platform_name != "nt":
        spawn_options["start_new_session"] = True
    popen_fn = subprocess.Popen if popen_fn is None else popen_fn
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            process = popen_fn(
                [
                    python_executable,
                    "-m",
                    "GemAgents",
                    "--workspace",
                    str(runner.workspace),
                    "--reconstruct-config",
                    str(config_path),
                ],
                cwd=runner.workspace,
                env=command_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **spawn_options,
            )
    except Exception as error:
        ledger.update(
            job_id,
            status="failed",
            phase="resume_spawn_failed",
            failure_reason="worker resume spawn failed",
        )
        raise ToolError("worker resume spawn failed") from error
    try:
        registered = ledger.register_process(job_id, process.pid)
    except Exception as error:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass
        ledger.update(
            job_id,
            status="failed",
            phase="resume_registration_failed",
            failure_reason="worker resume registration failed",
        )
        raise ToolError("worker resume registration failed") from error
    return json.dumps(
        {
            "job_id": job_id,
            "status": registered.get("status"),
            "phase": registered.get("phase"),
            "attempt_id": registered.get("attempt_id"),
            "resumed_from_phase": bundle.get("phase"),
        },
        ensure_ascii=False,
    )


__all__ = ["metabolic_resume", "metabolic_start"]
