"""Top-level deterministic reconstruction worker orchestration."""

from __future__ import annotations

import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class WorkerCancelled(Exception):
    """Internal signal used to publish a cooperative cancellation terminal state."""


def metabolic_pipeline(
    config: dict,
    workspace: Path,
    *,
    contract: Any | None = None,
    callbacks: Mapping[str, Any],
) -> dict:
    """Run the reconstruction worker through explicit deterministic callbacks."""
    from GemAgents.metabolic.reconstruction.pipeline_context import prepare_pipeline_context

    prepared = prepare_pipeline_context(
        config,
        workspace,
        contract=contract,
        validate_options=callbacks["validate_options"],
        compile_contract_fn=callbacks["compile_contract"],
        restricted_path_fn=callbacks["restricted_path"],
        string_options=callbacks["string_options"],
        artifact_store_factory=callbacks["artifact_store_factory"],
        source_ledger_fn=callbacks["source_ledger"],
        hash_path=callbacks["hash_path"],
        workflow_path=callbacks["workflow_path"],
        job_ledger_factory=callbacks["job_ledger_factory"],
    )
    config = prepared["config"]
    input_path = prepared["input_path"]
    out = prepared["out"]
    artifact_store = prepared["artifact_store"]
    manifest = prepared["manifest"]
    worker_job_id = prepared["worker_job_id"]
    worker_attempt_id = prepared.get("worker_attempt_id")
    worker_ledger = prepared["worker_ledger"]
    recovery_bundle = prepared.get("recovery_bundle")

    from GemAgents.metabolic.reconstruction.pipeline_phase import advance_phase

    write_json = callbacks["write_json"]
    hash_path = callbacks["hash_path"]
    now = callbacks.get("now", time.time)
    emit = callbacks.get("emit", lambda message: print(message, flush=True))

    def phase(name: str):
        return advance_phase(
            name,
            manifest=manifest,
            out=out,
            worker_ledger=worker_ledger,
            worker_job_id=worker_job_id,
            worker_attempt_id=worker_attempt_id,
            cancelled_error=WorkerCancelled,
            write_json=write_json,
            hash_path=hash_path,
            now=now,
            emit=emit,
        )

    heartbeat = None
    if worker_ledger is not None:
        heartbeat_factory = callbacks.get("heartbeat_factory")
        if heartbeat_factory is None:
            from GemAgents.metabolic.jobs.heartbeat import WorkerHeartbeat

            heartbeat_factory = WorkerHeartbeat
        heartbeat = heartbeat_factory(
            worker_ledger,
            worker_job_id,
            worker_attempt_id,
        )

    try:
        if heartbeat is not None:
            heartbeat.start()
        annotation_stage_override = None
        if recovery_bundle is not None:
            annotation_stage_override = callbacks["load_reaction_mapping_stage"](
                recovery_bundle,
                config=config,
                out=out,
                fasta_fn=callbacks["fasta"],
                native_universe_fn=callbacks["native_universe"],
                universe_fn=callbacks["universe"],
            )
        else:
            # Establish the first cancellable boundary before importing or
            # invoking optional scientific adapters. A cancellation request can
            # arrive while the worker is starting and must not be lost.
            phase("annotation")
        from cobra.io import write_sbml_model

        from GemAgents.metabolic.reconstruction.clean_inputs import prepare_clean_inputs
        from GemAgents.metabolic.reconstruction.pipeline_execution import (
            execute_pipeline_stages,
        )

        cpus = int(config.get("cpus", 4))
        executed = execute_pipeline_stages(
            config=config,
            workspace=workspace,
            input_path=input_path,
            out=out,
            manifest=manifest,
            artifact_store=artifact_store,
            phase=phase,
            cpus=cpus,
            write_sbml_model_fn=write_sbml_model,
            detect_input_fn=callbacks["detect_input"],
            run_pgap_fn=callbacks["run_pgap"],
            import_ncbi_fn=callbacks["import_ncbi"],
            predict_genes_fn=callbacks["predict_genes"],
            write_fasta_fn=callbacks["write_fasta"],
            fasta_fn=callbacks["fasta"],
            hash_path=hash_path,
            hmm_annotate_fn=callbacks["hmm_annotate"],
            prepare_clean_inputs_fn=prepare_clean_inputs,
            import_clean_predictions_fn=callbacks["import_clean_predictions"],
            write_json_fn=write_json,
            native_universe_fn=callbacks["native_universe"],
            universe_fn=callbacks["universe"],
            map_evidence_fn=callbacks["map_evidence"],
            mapping_audit_fn=callbacks.get("mapping_audit"),
            native_build_fn=callbacks["native_build"],
            build_model_fn=callbacks["build_model"],
            attach_gene_annotations_fn=callbacks["attach_gene_annotations"],
            enrich_memote_annotations_fn=callbacks["enrich_memote_annotations"],
            quality_fn=callbacks["quality"],
            build_universe_fn=callbacks["native_universe"],
            quality_runner=callbacks["quality"],
            reaction_evidence_ledger_fn=callbacks["reaction_evidence_ledger"],
            memote_runner=callbacks["memote"],
            copy_model_fn=callbacks.get("copy_model", shutil.copyfile),
            load_source_model_fn=callbacks["load_source_model"],
            annotation_stage_override=annotation_stage_override,
        )
        manifest = executed["manifest"]
        phase("finished")
        if worker_ledger is not None:
            worker_ledger.update(worker_job_id, status="completed", phase="finished")
        return manifest
    except Exception as error:
        if worker_ledger is not None and isinstance(error, WorkerCancelled):
            raise
        manifest.update(
            {
                "status": "failed",
                "execution_status": "failed",
                "benchmark_eligibility": "not_assessed",
                "failed_phase": manifest["phase"],
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        phase("failed")
        if worker_ledger is not None:
            worker_ledger.update(worker_job_id, status="failed", phase="failed")
        raise
    finally:
        if heartbeat is not None:
            heartbeat.stop()


__all__ = ["WorkerCancelled", "metabolic_pipeline"]
