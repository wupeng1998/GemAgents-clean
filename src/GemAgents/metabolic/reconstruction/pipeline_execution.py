"""Execute the deterministic reconstruction pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from GemAgents.errors import ToolError


def execute_pipeline_stages(
    *,
    config: dict,
    workspace: Path,
    input_path: Path,
    out: Path,
    manifest: dict,
    artifact_store,
    phase,
    cpus: int,
    write_sbml_model_fn,
    detect_input_fn,
    run_pgap_fn,
    import_ncbi_fn,
    predict_genes_fn,
    write_fasta_fn,
    fasta_fn,
    hash_path,
    hmm_annotate_fn,
    prepare_clean_inputs_fn,
    import_clean_predictions_fn,
    write_json_fn,
    native_universe_fn,
    universe_fn,
    map_evidence_fn,
    native_build_fn,
    build_model_fn,
    attach_gene_annotations_fn,
    enrich_memote_annotations_fn,
    quality_fn,
    build_universe_fn,
    quality_runner,
    reaction_evidence_ledger_fn,
    memote_runner,
    copy_model_fn,
    load_source_model_fn,
    mapping_audit_fn=None,
    annotation_stage_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run annotation, reconstruction, QC, evidence and final export stages.

    All domain operations are injected callbacks so the compatibility facade
    keeps its historical seams while the orchestration itself remains a
    deterministic reconstruction-layer implementation.
    """
    if config.get("kingdom", "bacteria") not in {"bacteria", "archaea"}:
        raise ToolError(
            "This NCBI workflow supports prokaryotes; "
            "eukaryotic reconstruction needs another frontend"
        )
    if config.get("engine", "native") not in {"native", "carveme", "reconstructor"}:
        raise ToolError("engine must be native, carveme or reconstructor")
    if not 1 <= cpus <= 128 or config.get("quality", "audit") not in {"audit", "repair"}:
        raise ToolError("Invalid CPU count or quality mode")

    from GemAgents.metabolic.reconstruction.pipeline_annotation import (
        run_annotation_and_mapping,
    )

    annotation_stage = annotation_stage_override or run_annotation_and_mapping(
        config=config,
        workspace=workspace,
        input_path=input_path,
        out=out,
        manifest=manifest,
        cpus=cpus,
        phase=phase,
        detect_input_fn=detect_input_fn,
        run_pgap_fn=run_pgap_fn,
        import_ncbi_fn=import_ncbi_fn,
        predict_genes_fn=predict_genes_fn,
        write_fasta_fn=write_fasta_fn,
        fasta_fn=fasta_fn,
        hash_path=hash_path,
        hmm_annotate_fn=hmm_annotate_fn,
        prepare_clean_inputs_fn=prepare_clean_inputs_fn,
        import_clean_predictions_fn=import_clean_predictions_fn,
        write_json_fn=write_json_fn,
        native_universe_fn=native_universe_fn,
        universe_fn=universe_fn,
        map_evidence_fn=map_evidence_fn,
        mapping_audit_fn=mapping_audit_fn,
        copy_file_fn=copy_model_fn,
    )
    # Annotation contains several independently expensive callbacks.  A
    # cancellation request can arrive after its entry boundary but before the
    # next reconstruction stage; re-check at the completed-stage boundary so
    # that such a request is not lost to a later validation failure.  A
    # recovered mapping stage is already complete, so preserve its phase
    # identity instead of writing a misleading annotation checkpoint.
    phase("reaction_mapping" if annotation_stage_override is not None else "annotation")
    annotation = annotation_stage["annotation"]
    proteins = annotation_stage["proteins"]
    evidence = annotation_stage["evidence"]
    gene_ids = annotation_stage["gene_ids"]
    clean_model_ids = annotation_stage["clean_model_ids"]
    universal = annotation_stage["universal"]
    universe_path = annotation_stage["universe_path"]
    mapped = annotation_stage["mapped"]

    from GemAgents.metabolic.reconstruction.pipeline_reconstruction import (
        run_reconstruction_stage,
    )

    reconstruction_stage = run_reconstruction_stage(
        config=config,
        out=out,
        manifest=manifest,
        phase=phase,
        universal=universal,
        universe_path=universe_path,
        mapped=mapped,
        evidence=evidence,
        proteins=proteins,
        gene_ids=gene_ids,
        artifact_store=artifact_store,
        native_build_fn=native_build_fn,
        build_model_fn=build_model_fn,
        attach_gene_annotations_fn=attach_gene_annotations_fn,
        enrich_memote_annotations_fn=enrich_memote_annotations_fn,
        write_sbml_model_fn=write_sbml_model_fn,
    )
    model = reconstruction_stage["model"]
    biomass = reconstruction_stage["biomass"]
    gapfilled_artifact = reconstruction_stage["gapfilled_artifact"]
    gene_annotation_summary = reconstruction_stage["gene_annotation_summary"]

    from GemAgents.metabolic.reconstruction.pipeline_quality import run_quality_stage

    quality_stage = run_quality_stage(
        config=config,
        out=out,
        manifest=manifest,
        phase=phase,
        model=model,
        biomass=biomass,
        gapfilled_artifact=gapfilled_artifact,
        artifact_store=artifact_store,
        quality_fn=quality_fn,
    )
    model = quality_stage["model"]
    quality = quality_stage["quality"]
    current_artifact = quality_stage["current_artifact"]
    current_attempt = quality_stage["current_attempt"]

    from GemAgents.metabolic.reconstruction.qc_feedback import run_qc_feedback

    (
        model,
        biomass,
        quality,
        current_artifact,
        current_attempt,
        _qc_feedback,
    ) = run_qc_feedback(
        config=config,
        out=out,
        artifact_store=artifact_store,
        model=model,
        biomass=biomass,
        quality=quality,
        current_artifact=current_artifact,
        current_attempt=current_attempt,
        mapped=mapped,
        evidence=evidence,
        manifest=manifest,
        build_universe=build_universe_fn,
        build_model=native_build_fn,
        quality_runner=quality_runner,
    )

    from GemAgents.metabolic.reconstruction.pipeline_evidence import record_quality_evidence

    gene_annotation_summary = record_quality_evidence(
        model=model,
        evidence=evidence,
        proteins=proteins,
        gene_ids=gene_ids,
        annotation=annotation,
        quality=quality,
        manifest=manifest,
        out=out,
        write_json=write_json_fn,
        attach_gene_annotations=attach_gene_annotations_fn,
        enrich_memote_annotations=enrich_memote_annotations_fn,
        reaction_evidence_ledger=reaction_evidence_ledger_fn,
    )

    from GemAgents.metabolic.reconstruction.pipeline_finalize import finalize_pipeline_artifact

    final_artifact, quality = finalize_pipeline_artifact(
        model=model,
        quality=quality,
        config=config,
        out=out,
        artifact_store=artifact_store,
        current_artifact=current_artifact,
        current_attempt=current_attempt,
        manifest=manifest,
        gene_annotation_summary=gene_annotation_summary,
        clean_model_ids=clean_model_ids,
        phase=phase,
        memote_runner=memote_runner,
        write_json=write_json_fn,
        hash_path=hash_path,
        copy_model=copy_model_fn,
    )

    from GemAgents.metabolic.reconstruction.pipeline_reference import (
        record_reference_comparison,
    )

    record_reference_comparison(
        config=config,
        out=out,
        model=model,
        quality=quality,
        manifest=manifest,
        load_source_model=load_source_model_fn,
    )
    manifest.update(execution_status="completed", model_status="exported_draft")
    return {"manifest": manifest, "final_artifact": final_artifact, "quality": quality}
