"""Reconstruction-stage model build and immutable artifact registration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def run_reconstruction_stage(
    *,
    config: dict,
    out: Path,
    manifest: dict,
    phase,
    universal,
    universe_path: Path,
    mapped: dict,
    evidence: list[dict],
    proteins: list[tuple[str, str]],
    gene_ids: dict[str, str],
    artifact_store,
    native_build_fn,
    build_model_fn,
    attach_gene_annotations_fn,
    enrich_memote_annotations_fn,
    write_sbml_model_fn,
) -> dict[str, Any]:
    """Build a model, record its reconstruction attempt, and write draft.xml."""
    phase("reconstruction")
    model, biomass = (
        native_build_fn(universal, universe_path, mapped, evidence, config, out)
        if config.get("engine") == "native"
        else build_model_fn(universal, universe_path, mapped, config, out)
    )
    build_attempt = artifact_store.begin_attempt("reconstruction", None)
    initial_artifact = (
        artifact_store.import_model(
            out / "initial_model.xml", "initial", "raw", attempt=build_attempt
        )
        if (out / "initial_model.xml").is_file()
        else None
    )
    gapfilled_artifact = artifact_store.write_model(
        model,
        "gapfilled",
        "raw",
        parent_artifact_id=initial_artifact.artifact_id if initial_artifact else None,
        attempt=build_attempt,
    )
    artifact_store.finish_attempt(
        build_attempt,
        status="completed",
        gate_passed=True,
        artifact_ids=tuple(
            artifact.artifact_id
            for artifact in (initial_artifact, gapfilled_artifact)
            if artifact is not None
        ),
    )
    manifest["model_artifacts"] = {
        "initial": initial_artifact.artifact_id if initial_artifact else None,
        "gapfilled": gapfilled_artifact.artifact_id,
    }
    gene_annotation_summary = attach_gene_annotations_fn(model, evidence, proteins, gene_ids)
    manifest["memote_annotation"] = enrich_memote_annotations_fn(model)
    manifest["gene_annotation"] = gene_annotation_summary
    if config.get("engine") == "native" and (out / "gapfill-report.json").is_file():
        gapfill_metadata = json.loads((out / "gapfill-report.json").read_text(encoding="utf-8"))
        manifest["medium_assumption"] = {
            "input": config.get("medium_file") or config.get("medium", "minimal"),
            "canonical_exchanges": gapfill_metadata.get("medium", {}),
            "growth_scenarios": gapfill_metadata.get("growth_scenarios", []),
        }
    model.id = "ncbi_evidence_model"
    # Compatibility snapshot: this immutable gapfilled draft is never overwritten.
    write_sbml_model_fn(model, str(out / "draft.xml"))
    return {
        "model": model,
        "biomass": biomass,
        "gapfilled_artifact": gapfilled_artifact,
        "gene_annotation_summary": gene_annotation_summary,
    }
