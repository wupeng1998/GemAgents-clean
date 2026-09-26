"""Native reconstruction initial-model and scenario setup."""

from __future__ import annotations

from pathlib import Path

from cobra.io import write_sbml_model

from GemAgents.metabolic.library.quality import load_reaction_quality_table
from GemAgents.metabolic.media.selection import load_declared_medium
from GemAgents.metabolic.reconstruction.evidence_selection import (
    select_reconstruction_evidence,
)
from GemAgents.metabolic.reconstruction.initial_model import assemble_initial_model
from GemAgents.metabolic.reconstruction.native_scenarios import (
    prepare_native_growth_scenarios,
)


def prepare_native_initial_setup(
    *,
    universal,
    mapped: dict,
    config: dict,
    reaction_library_path: Path,
    reference_candidate_meta: dict[str, dict],
    template_support_ids: set[str],
    template_medium_support_ids: set[str],
    biomass,
    biomass_id: str,
    biomass_product_drain_ids: set[str],
    reference_exchanges: dict,
    reference_prefix: str,
    reference_support_path: Path,
    reference_scenarios_fn,
    oxygen_exchange_ids_fn,
    close_transport_fn,
    out: Path,
    reference_candidate_ids: set[str] | None = None,
) -> dict:
    """Prepare the strict initial model and growth scenarios before gap-fill."""
    medium = load_declared_medium(config)
    quality = load_reaction_quality_table(reaction_library_path / "reaction_quality.tsv")
    for reaction_id, metadata in reference_candidate_meta.items():
        quality[reaction_id] = {
            "status": "reference_candidate",
            "active": "true",
            "qc_status": "public_reference_chemistry_preserved",
            "gpr_supported": str(bool(metadata.get("gpr"))).lower(),
        }
    quality, strict_evidence, annotation_evidence = select_reconstruction_evidence(
        universal,
        quality,
        mapped,
        template_support_ids,
        template_medium_support_ids,
    )
    # A small, explicit public-reaction rescue tier is allowed for biomass
    # precursors that are represented in BiGG/CarveMe but fail strict charge
    # bookkeeping.  Keep the library QC result intact and record the override
    # in the run configuration so these reactions remain auditable.
    for reaction_id in config.get("growth_rescue_reactions", []):
        if reaction_id not in universal.reactions:
            continue
        row = quality.get(reaction_id)
        if row is None or row.get("status") not in {
            "mass_or_charge_imbalance",
            "unknown_formula_or_charge",
        }:
            continue
        row.update(
            status="rescue_for_growth",
            active="true",
            qc_status="explicit_public_reaction_growth_rescue",
        )
    initial, selected_universal_medium, selected_medium = assemble_initial_model(
        universal,
        medium,
        strict_evidence,
        template_medium_support_ids,
        biomass,
        biomass_id,
        biomass_product_drain_ids,
        reference_exchanges,
        reference_prefix,
    )
    # Seed the initial model from public reference candidates while retaining
    # the normal LP gap-fill/finalization path. ``reference_scaffold`` remains
    # the explicit carry-forward mode that skips that optimizer.
    if config.get("reference_scaffold"):
        # A reaction already present in the universal model is canonicalized
        # by the reaction library.  Reference metadata may carry an older
        # bound (for example, a reversible PPK from a published model), but
        # applying it here would silently undo the library direction QC before
        # the scaffold is optimized.  Only reactions imported under a new
        # reference-prefixed ID may take their bounds from the source model.
        library_ids = {reaction.id for reaction in universal.reactions}
        scaffold_ids = set(reference_candidate_ids or reference_candidate_meta)
        scaffold_ids.update(
            details.get("target_id", source_id)
            for source_id, details in reference_exchanges.items()
        )
        initial.add_reactions(
            [
                universal.reactions.get_by_id(rid).copy()
                for rid in sorted(scaffold_ids)
                if rid in universal.reactions and rid not in initial.reactions
            ]
        )
        for reaction_id in scaffold_ids:
            if reaction_id not in initial.reactions:
                continue
            reaction = initial.reactions.get_by_id(reaction_id)
            metadata = reference_candidate_meta.get(reaction_id, {})
            if metadata.get("gpr"):
                reaction.gene_reaction_rule = metadata["gpr"]
            if metadata.get("bounds") and reaction_id not in library_ids:
                reaction.bounds = tuple(float(value) for value in metadata["bounds"])
            reaction.notes["evidence_status"] = (
                "public_reference_gpr" if metadata.get("gpr") else "public_reference_scaffold"
            )
            reaction.notes["reference_scaffold"] = "true"
        initial.objective = biomass_id
        initial.medium = {
            rid: rate for rid, rate in selected_medium.items() if rid in initial.reactions
        }
    minimum_growth = float(config.get("min_growth", 0.01))
    growth_scenarios = prepare_native_growth_scenarios(
        initial=initial,
        universal=universal,
        reference_support_path=reference_support_path,
        medium=medium,
        minimum_growth=minimum_growth,
        reference_support=config.get("reference_support", True),
        selected_medium=selected_medium,
        template_medium_support_ids=template_medium_support_ids,
        mapped=mapped,
        reference_growth_ceiling=bool(config.get("reference_growth_ceiling", False)),
        reference_scenarios_fn=reference_scenarios_fn,
        oxygen_exchange_ids_fn=oxygen_exchange_ids_fn,
        close_transport_fn=close_transport_fn,
    )
    # Transport closure may add native reactions after the scaffold is
    # assembled.  Apply source-only bounds last so closure cannot reopen a
    # native shortcut that this reference-assisted track intentionally excludes.
    if config.get("reference_scaffold") and config.get("reference_scaffold_source_only", False):
        reference_ids = set(reference_candidate_ids or reference_candidate_meta)
        reference_ids.update(
            details.get("target_id", source_id)
            for source_id, details in reference_exchanges.items()
        )
        for reaction in initial.reactions:
            if reaction.id in reference_ids or reaction.id == biomass_id:
                continue
            if reaction.boundary and reaction.id in selected_medium:
                continue
            reaction.bounds = (0.0, 0.0)
    write_sbml_model(initial, str(out / "initial_model.xml"))
    return {
        "medium": medium,
        "quality": quality,
        "strict_evidence": strict_evidence,
        "annotation_evidence": annotation_evidence,
        "initial": initial,
        "selected_universal_medium": selected_universal_medium,
        "selected_medium": selected_medium,
        "minimum_growth": minimum_growth,
        "growth_scenarios": growth_scenarios,
    }
