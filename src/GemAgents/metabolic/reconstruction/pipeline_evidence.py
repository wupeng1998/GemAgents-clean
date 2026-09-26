"""Persist quality-derived annotation and reaction evidence for a pipeline run."""

from __future__ import annotations

from pathlib import Path


def record_quality_evidence(
    *,
    model,
    evidence: list[dict],
    proteins: list[tuple[str, str]],
    gene_ids: dict[str, str],
    annotation: str,
    quality: dict,
    manifest: dict,
    out: Path,
    write_json,
    attach_gene_annotations,
    enrich_memote_annotations,
    reaction_evidence_ledger,
) -> dict:
    """Recalculate final annotations and persist the reaction evidence ledger."""
    # Quality repair may change the reaction set. Recheck membership and
    # recalculate counts on the final model before SBML/MEMOTE export.
    gene_annotation_summary = attach_gene_annotations(model, evidence, proteins, gene_ids)
    manifest["gene_annotation"] = gene_annotation_summary
    # Complete the evidence funnel started during reaction mapping.  These
    # counts intentionally describe different populations: a mapped candidate
    # can be discarded by QC/gap-fill, while a final GPR gene must occur in a
    # reaction retained by the exported model.
    reconciliation = manifest.setdefault("evidence_reconciliation", {})
    reconciliation.update(
        {
            "final_model_reactions": len(model.reactions),
            "final_model_reactions_with_gpr": sum(
                bool(reaction.gene_reaction_rule) for reaction in model.reactions
            ),
            "final_model_gpr_genes": gene_annotation_summary["gpr_genes"],
            "final_model_genes": len(model.genes),
        }
    )
    manifest["memote_annotation"] = enrich_memote_annotations(model)
    write_json(out / "quality.json", quality)
    model.notes["annotation_method"] = annotation
    model.notes["quality_status"] = quality["status"]
    model.notes["scope"] = "Evidence-guided draft; EC mapping and stated probe coverage only"
    reaction_ledger = reaction_evidence_ledger(model)
    write_json(out / "reaction-evidence-ledger.json", reaction_ledger)
    manifest["reaction_evidence"] = {
        "artifact": str(out / "reaction-evidence-ledger.json"),
        "reactions": len(reaction_ledger),
        "unknown_chemistry": sum(
            row["evidence_class"] == "unknown_chemistry" for row in reaction_ledger
        ),
        "complete_evidence_ids": all(row["evidence_id"] for row in reaction_ledger),
    }
    source_blocked = any(
        row["status"] in {"BLOCKED_POLICY", "BLOCKED_ASSET"}
        for row in manifest["source_ledger"]
    )
    manifest["source_policy_status"] = (
        "BLOCKED_PROVENANCE"
        if source_blocked or manifest["reaction_evidence"]["unknown_chemistry"]
        else "DECLARED_UNVERIFIED"
    )
    return gene_annotation_summary
