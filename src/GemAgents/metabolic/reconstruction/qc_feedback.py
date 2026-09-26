"""Bounded quality-feedback retries for native reconstruction."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.contracts import ReconstructionContext


def run_qc_feedback(
    *,
    config: dict,
    out: Path,
    artifact_store,
    model,
    biomass: str,
    quality: dict,
    current_artifact,
    current_attempt,
    mapped: dict,
    evidence: list[dict],
    manifest: dict,
    build_universe,
    build_model,
    quality_runner,
):
    """Retry native gap filling with forbidden directions found by failed probes."""
    qc_feedback = []
    forbidden_directions: set[tuple[str, str]] = set()
    if config.get("engine") == "native" and config.get("quality", "audit") == "repair":
        for feedback_round in range(1, 3):
            if quality.get("declared_checks_passed"):
                break
            gapfill_path = out / "gapfill-report.json"
            if not gapfill_path.is_file():
                raise ToolError(
                    "QC feedback cannot continue: gapfill-report.json is missing "
                    f"from {out}"
                )
            gapfill = json.loads(gapfill_path.read_text(encoding="utf-8"))
            additions = set(gapfill.get("additions", []))
            new_cuts = {
                (reaction_id, "+" if float(flux) > 0 else "-")
                for probe in quality.get("final_probes", [])
                if probe.get("status") == "fail"
                for reaction_id, flux in probe.get("witness", {}).items()
                if reaction_id in additions and abs(float(flux)) > 1e-12
            } - forbidden_directions
            if not new_cuts:
                break
            forbidden_directions.update(new_cuts)
            feedback = {
                "round": feedback_round,
                "cuts": [list(item) for item in sorted(new_cuts)],
                "source_probe_status": {
                    probe["name"]: probe["status"] for probe in quality.get("final_probes", [])
                },
            }
            retry_config = dict(config)
            retry_context = ReconstructionContext(tuple(sorted(forbidden_directions)))
            retry_universal, retry_path = build_universe(retry_config)
            retry_attempt = artifact_store.begin_attempt(
                "qc_feedback", current_artifact.artifact_id
            )
            retry_out = retry_attempt.path / "work"
            retry_out.mkdir()
            try:
                # Native reconstruction reads the staged protein FASTA from
                # the attempt workspace.  A QC retry changes only forbidden
                # directions; preserve the exact input snapshot instead of
                # asking the annotation stage to run again.
                proteins_path = out / "proteins.faa"
                if not proteins_path.is_file():
                    raise ToolError("QC feedback retry is missing proteins.faa")
                shutil.copy2(proteins_path, retry_out / "proteins.faa")
                retry_model, retry_biomass = build_model(
                    retry_universal,
                    retry_path,
                    copy.deepcopy(mapped),
                    evidence,
                    retry_config,
                    retry_out,
                    context=retry_context,
                )
            except ToolError as error:
                failure_path = retry_out / "gapfill-report.json"
                failure = (
                    json.loads(failure_path.read_text()) if failure_path.is_file() else {}
                )
                feedback.update(status=failure.get("status", "internal_error"), error=str(error))
                qc_feedback.append(feedback)
                artifact_store.finish_attempt(
                    retry_attempt,
                    status="failed",
                    gate_passed=False,
                    reason=str(error),
                )
                break
            retry_model.id = "ncbi_evidence_model"
            retry_model, retry_quality = quality_runner(
                retry_model, retry_biomass, retry_config
            )
            retry_artifact = artifact_store.write_model(
                retry_model,
                "repaired",
                "exploratory",
                parent_artifact_id=current_artifact.artifact_id,
                attempt=retry_attempt,
            )
            retry_passed = bool(retry_quality.get("declared_checks_passed"))
            artifact_store.write_patch(
                retry_attempt,
                {
                    "parent_artifact_id": current_artifact.artifact_id,
                    "before_semantic_hash": current_artifact.semantic_hash,
                    "after_semantic_hash": retry_artifact.semantic_hash,
                    "changes": [
                        {"reaction_id": rid, "forbidden_direction": sign}
                        for rid, sign in sorted(new_cuts)
                    ],
                    "tasks_before": quality.get("final_tasks", []),
                    "tasks_after": retry_quality.get("final_tasks", []),
                    "probes_before": quality.get("final_probes", []),
                    "probes_after": retry_quality.get("final_probes", []),
                    "decision": "accepted" if retry_passed else "rejected",
                    "reason": retry_quality.get("status", "quality status unavailable"),
                },
            )
            artifact_store.finish_attempt(
                retry_attempt,
                status="completed",
                gate_passed=retry_passed,
                artifact_ids=(retry_artifact.artifact_id,),
                reason=retry_quality.get("status"),
            )
            feedback.update(
                status="completed",
                final_probe_status={
                    probe["name"]: probe["status"]
                    for probe in retry_quality.get("final_probes", [])
                },
            )
            qc_feedback.append(feedback)
            model, biomass, quality = retry_model, retry_biomass, retry_quality
            current_artifact = retry_artifact
            current_attempt = retry_attempt
        quality["gapfill_qc_feedback"] = qc_feedback
        manifest["gapfill_qc_feedback"] = qc_feedback
    return model, biomass, quality, current_artifact, current_attempt, qc_feedback
