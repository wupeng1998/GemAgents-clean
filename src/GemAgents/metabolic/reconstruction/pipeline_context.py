"""Deterministic pipeline configuration and provenance context preparation."""

from __future__ import annotations

import os
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from GemAgents.contracts import (
    apply_reconstruction_defaults,
    default_biomass_library_path,
    default_reaction_library_path,
)
from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout


def prepare_pipeline_context(
    config: dict,
    workspace: Path,
    *,
    contract: Any | None,
    validate_options,
    compile_contract_fn,
    restricted_path_fn,
    string_options: set[str],
    artifact_store_factory,
    source_ledger_fn,
    hash_path,
    workflow_path: Path,
    job_ledger_factory,
    now=time.time,
) -> dict[str, Any]:
    """Validate and materialize the immutable state needed by the pipeline.

    This leaf performs no annotation, reconstruction, solver or model mutation.
    It only resolves user paths, checks the contract, creates the output run
    directory and records the initial provenance/worker context.
    """
    config = apply_reconstruction_defaults(config, workspace)
    if contract is not None:
        config_path = Path(contract.config_path) if contract.config_path else None
        verified = compile_contract_fn(config, workspace, config_path=config_path)
        if verified.contract_id != contract.contract_id:
            raise ToolError("reconstruction contract changed before pipeline start")
    validate_options(config)
    for key in string_options - {"organism", "pgap_memory", "biomass_template"}:
        if config.get(key) and restricted_path_fn(workspace / config[key]):
            raise ToolError(f"BLOCKED_POLICY: restricted {key}")

    resolved_config = dict(config)
    layout = RepoLayout(workspace)
    resolved_config.setdefault("engine", "native")
    if resolved_config["engine"] == "native":
        resolved_config.setdefault("reaction_library", default_reaction_library_path(workspace))
        resolved_config.setdefault("biomass_library", default_biomass_library_path(workspace))
        resolved_config.setdefault("reference_support", False)
    pgap_runtime = layout.assets / "pgap/runtime.json"
    if not resolved_config.get("pgap_config") and pgap_runtime.is_file():
        resolved_config["pgap_config"] = str(pgap_runtime)
    if resolved_config["engine"] == "reconstructor" and not resolved_config.get(
        "reaction_library"
    ):
        resolved_config.setdefault("modelseed_reactions", "data/ncbi_hmm/modelseed_reactions.tsv")
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
        "reference_protein_fasta",
    ):
        if resolved_config.get(name):
            path = Path(str(resolved_config[name])).expanduser()
            resolved_config[name] = str(
                (workspace / path).resolve() if not path.is_absolute() else path.resolve()
            )
    if not resolved_config.get("input"):
        raise ToolError("Reconstruction config requires an input FAA/FNA path")
    input_path = Path(resolved_config["input"])
    if not input_path.is_file():
        raise ToolError(f"Input genome does not exist: {input_path}")
    try:
        out = layout.writable(
            resolved_config.get("output", layout.runs / f"gem_{uuid.uuid4().hex[:10]}")
        )
    except (PermissionError, ValueError) as error:
        raise ToolError(f"Output path rejected: {error}") from error
    resolved_config["output"] = str(out)
    worker_job_id = os.environ.get("GEMAGENTS_JOB_ID")
    worker_attempt_id = os.environ.get("GEMAGENTS_JOB_ATTEMPT_ID")
    resume_job_id = os.environ.get("GEMAGENTS_RESUME_JOB_ID")
    worker_ledger = job_ledger_factory(workspace) if worker_job_id else None
    recovery_bundle = None
    if worker_ledger is not None:
        try:
            worker_ledger.wait_for_worker_registration(
                worker_job_id,
                worker_attempt_id,
                pid=os.getpid(),
            )
        except (KeyError, OSError, ValueError) as error:
            raise ToolError(f"worker attempt identity is invalid: {error}") from error
    if resume_job_id:
        if worker_ledger is None or resume_job_id != worker_job_id:
            raise ToolError("resume worker identity does not match the job ledger")
        try:
            recovery_record = worker_ledger.load(resume_job_id)
            if recovery_record.get("status") != "running" or recovery_record.get(
                "resume_pending"
            ) is not True:
                raise ToolError("job is not marked for a pending reconstruction resume")
            if Path(str(recovery_record.get("output", ""))).resolve() != out.resolve():
                raise ToolError("resume output does not match the job ledger")
            recovery_bundle = worker_ledger.load_recovery_artifacts(
                resume_job_id, content_key=str(recovery_record["content_key"])
            )
        except (KeyError, OSError, ValueError) as error:
            raise ToolError(f"recovery checkpoint cannot be loaded: {error}") from error
    if out.exists() and any(out.iterdir()) and recovery_bundle is None:
        raise ToolError(
            "Output directory is not empty; use a new run directory to avoid stale artifacts"
        )
    out.mkdir(parents=True, exist_ok=True)
    artifact_store = artifact_store_factory(out)

    source_items = []
    for key, source_class in (
        ("input", "sequence_input"),
        ("annotation_gbk", "annotation_record"),
        ("clean_predictions", "clean_predictions"),
        ("reference_support_path", "reference_model"),
        ("reference_protein_fasta", "reference_protein_fasta"),
    ):
        if resolved_config.get(key):
            source_items.append(
                {
                    "path": resolved_config[key],
                    "source_id": key,
                    "source_class": source_class,
                    "license_or_usage_terms": "declared_by_configuration",
                    "allowed_tracks": ("reference_assisted",)
                    if key == "reference_support_path"
                    else ("de_novo_public", "biomass_controlled", "reference_assisted"),
                }
            )
    for key, source_class in (
        ("reaction_library", "reaction_library"),
        ("biomass_library", "biomass_library"),
        ("hmm_dir", "annotation_database"),
    ):
        if resolved_config.get(key):
            target = Path(resolved_config[key])
            target = target / "manifest.json" if target.is_dir() else target
            source_items.append(
                {
                    "path": target,
                    "source_id": key,
                    "source_class": source_class,
                    "license_or_usage_terms": "declared_by_configuration",
                    "allowed_tracks": (
                        "de_novo_public",
                        "biomass_controlled",
                        "reference_assisted",
                    ),
                }
            )

    manifest = {
        "status": "running",
        "execution_status": "running",
        "model_status": "not_built",
        "validation_status": {"status": "not_run", "checks": []},
        "benchmark_eligibility": "not_assessed",
        "phase": "validation",
        "config": resolved_config,
        "evaluation_track": resolved_config.get("evaluation_track", "unclassified"),
        "source_ledger": source_ledger_fn(source_items),
        "input_sha256": hash_path(input_path),
        "workflow_sha256": hash_path(workflow_path),
        "started_unix": now(),
        "output": str(out),
        "contract_id": contract.contract_id if contract is not None else None,
        "llm_used_for_annotation": False,
        "source_policy_status": "unverified_dependencies",
        "prohibited_projects_used": None,
        "versions": {},
    }
    if recovery_bundle is not None:
        recovered_manifest = recovery_bundle.get("manifest")
        if not isinstance(recovered_manifest, dict):
            raise ToolError("recovery checkpoint manifest is invalid")
        if recovered_manifest.get("contract_id") != manifest.get("contract_id"):
            raise ToolError("recovery checkpoint contract identity changed")
        if recovered_manifest.get("input_sha256") != manifest.get("input_sha256"):
            raise ToolError("recovery checkpoint input identity changed")
        manifest = recovered_manifest
        if manifest.get("phase") != "reaction_mapping":
            raise ToolError(
                "only a reaction_mapping checkpoint can resume the deterministic pipeline"
            )
    for package in ("gemagents", "cobra", "carveme", "reconstructor", "pyhmmer", "pyrodigal"):
        try:
            manifest["versions"][package] = version(package)
        except PackageNotFoundError:
            pass
    return {
        "config": resolved_config,
        "input_path": input_path,
        "out": out,
        "artifact_store": artifact_store,
        "manifest": manifest,
        "worker_job_id": worker_job_id,
        "worker_attempt_id": worker_attempt_id,
        "worker_ledger": worker_ledger,
        "recovery_bundle": recovery_bundle,
    }
