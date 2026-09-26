"""Evidence-tiered candidate-pool construction for native gap filling."""

from __future__ import annotations


def build_gapfill_candidate_pools(
    universal,
    mapped: dict,
    quality: dict,
    strict_evidence: set[str],
    annotation_evidence: set[str],
    reference_candidate_ids: set[str],
    biomass_id: str,
    selected_universal_medium: set[str],
    config: dict,
) -> dict[str, object]:
    """Build ordered evidence tiers and guarded bounds without solving."""
    candidate_ids = {
        reaction.id
        for reaction in universal.reactions
        if (
            reaction.id in reference_candidate_ids
            or (
                reaction.id in (strict_evidence | annotation_evidence)
                and quality.get(reaction.id, {}).get("status") in {"pass", "rescue_for_growth"}
            )
        )
        and (not reaction.boundary or reaction.id in reference_candidate_ids)
        and reaction.id != biomass_id
        and reaction.id.lower() not in {"growth", "biomass"}
        and "biomass" not in reaction.id.lower()
    }
    template_gapfill_ids = {
        reaction_id
        for reaction_id, row in quality.items()
        if (
            row.get("status") in {"pass", "rescue_for_growth"}
            and row.get("active", "true").lower() == "true"
            and reaction_id in universal.reactions
            and reaction_id not in candidate_ids
            and reaction_id != biomass_id
            and not universal.reactions.get_by_id(reaction_id).boundary
            and "biomass" not in reaction_id.lower()
        )
    }
    candidate_ids.update(template_gapfill_ids)
    unverified_gapfill_ids = {
        reaction_id
        for reaction_id, row in quality.items()
        if (
            config.get("allow_unverified_gapfill", False)
            and row.get("status") == "unknown_formula_or_charge"
            and reaction_id in universal.reactions
            and reaction_id not in candidate_ids
            and reaction_id != biomass_id
            and not universal.reactions.get_by_id(reaction_id).boundary
            and bool(universal.reactions.get_by_id(reaction_id).annotation.get("bigg.reaction"))
            and "biomass" not in reaction_id.lower()
            and (
                config.get("kingdom", "bacteria") not in {"bacteria", "archaea"}
                or {
                    metabolite.id.rsplit("_", 1)[-1]
                    for metabolite in universal.reactions.get_by_id(reaction_id).metabolites
                }
                <= {"c", "p", "e"}
            )
        )
    }
    boundary_gapfill_ids = {
        reaction.id
        for reaction in universal.boundary
        if reaction.id not in selected_universal_medium
    }
    candidate_ids.update(unverified_gapfill_ids)
    candidate_ids.update(boundary_gapfill_ids)

    def guarded_unverified_bounds(reaction) -> tuple[float, float]:
        bounds = reaction.bounds
        stoichiometry = {
            metabolite.id: coefficient for metabolite, coefficient in reaction.metabolites.items()
        }
        for carrier, low in (("atp_c", "adp_c"), ("gtp_c", "gdp_c")):
            hydrolysis = {
                carrier: -1.0,
                "h2o_c": -1.0,
                low: 1.0,
                "pi_c": 1.0,
                "h_c": 1.0,
            }
            if stoichiometry == hydrolysis:
                return max(0.0, bounds[0]), bounds[1]
            reverse_hydrolysis = {
                metabolite: -coefficient for metabolite, coefficient in hydrolysis.items()
            }
            if stoichiometry == reverse_hydrolysis:
                return bounds[0], min(0.0, bounds[1])
        return bounds

    template_bounds = {
        rid: universal.reactions.get_by_id(rid).bounds
        for rid in template_gapfill_ids
        if rid in universal.reactions
    }
    unverified_bounds = {
        rid: guarded_unverified_bounds(universal.reactions.get_by_id(rid))
        for rid in unverified_gapfill_ids
        if rid in universal.reactions
    }
    return {
        "candidate_ids": candidate_ids,
        "template_gapfill_ids": template_gapfill_ids,
        "unverified_gapfill_ids": unverified_gapfill_ids,
        "boundary_gapfill_ids": boundary_gapfill_ids,
        "template_bounds": template_bounds,
        "unverified_bounds": unverified_bounds,
    }
