"""Final artifact export and quality/MEMOTE manifest assembly."""

from __future__ import annotations

from pathlib import Path


def finalize_pipeline_artifact(
    *,
    model,
    quality: dict,
    config: dict,
    out: Path,
    artifact_store,
    current_artifact,
    current_attempt,
    manifest: dict,
    gene_annotation_summary: dict,
    clean_model_ids: set[str],
    phase,
    memote_runner,
    write_json,
    hash_path,
    copy_model,
):
    """Write final model artifacts, close the attempt and update run status."""
    final_role = (
        "calibration" if quality.get("growth_calibration", {}).get("applied") else "raw"
    )
    final_artifact = artifact_store.write_model(
        model,
        "final",
        final_role,
        parent_artifact_id=current_artifact.artifact_id,
        attempt=current_attempt,
    )
    copy_model(final_artifact.model_path, out / "model.xml")
    final_gate_passed = bool(quality.get("declared_checks_passed"))
    artifact_store.finish_attempt(
        current_attempt,
        status="completed",
        gate_passed=final_gate_passed,
        artifact_ids=(current_artifact.artifact_id, final_artifact.artifact_id),
        reason=quality.get("status"),
    )
    if final_gate_passed:
        artifact_store.promote(current_attempt, final_artifact)
    manifest["model_artifacts"].update(
        {
            "repaired": current_artifact.artifact_id,
            "final": final_artifact.artifact_id,
            "accepted": final_artifact.artifact_id if final_gate_passed else None,
            "final_role": final_role,
        }
    )
    if config.get("memote", True):
        phase("memote")
        manifest["memote"] = memote_runner(out / "model.xml", out / "memote", config)
    else:
        manifest["memote"] = {"status": "skipped", "score_percent": None}
    quality["memote"] = manifest["memote"]
    write_json(out / "quality.json", quality)
    manifest.update(
        {
            "status": "completed"
            if quality["declared_checks_passed"]
            and manifest["memote"]["status"] in {"completed", "skipped"}
            else "completed_with_findings",
            "model": str(out / "model.xml"),
            "reactions": len(model.reactions),
            "metabolites": len(model.metabolites),
            "genes": len(model.genes),
            "gpr_genes": gene_annotation_summary["gpr_genes"],
            "annotated_genes": gene_annotation_summary["annotated_genes"],
            "quality_status": quality["status"],
            "declared_checks_passed": quality["declared_checks_passed"],
            "model_stage": "draft_requires_biological_validation",
            "model_sha256": hash_path(out / "model.xml"),
        }
    )
    manifest["clean_prediction"]["model_genes"] = len(
        {gene.id for gene in model.genes if gene.id in clean_model_ids}
    )
    manifest.update(
        {
            key: quality[key]
            for key in (
                "validation_status",
                "benchmark_eligibility",
                "repair_search_status",
                "final_audit_status",
            )
        }
    )
    manifest.update(execution_status="completed", model_status="exported_draft")
    return final_artifact, quality
