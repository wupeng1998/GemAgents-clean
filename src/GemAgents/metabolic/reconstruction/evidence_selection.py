"""Evidence and quality selection for native reconstruction."""

from __future__ import annotations


def select_reconstruction_evidence(
    universal,
    quality: dict,
    mapped: dict,
    template_support_ids: set[str],
    template_medium_support_ids: set[str],
) -> tuple[dict, set[str], set[str]]:
    """Recheck normalized chemistry and derive strict evidence pools."""
    normalized_reactions = {
        reaction
        for metabolite in universal.metabolites
        if "reference_chemistry_normalization" in metabolite.notes
        for reaction in metabolite.reactions
        if reaction.id in universal.reactions and not reaction.boundary
    }
    for reaction in normalized_reactions:
        row = quality.get(reaction.id)
        if row is None:
            continue
        if reaction.check_mass_balance():
            row.update(status="mass_or_charge_imbalance", active="false")
            template_support_ids.discard(reaction.id)
            template_medium_support_ids.discard(reaction.id)
        elif row.get("status") == "mass_or_charge_imbalance":
            row.update(status="pass", active="true", qc_status="reference_chemistry_rechecked")

    strict_evidence = {
        rid
        for rid, entry in mapped.items()
        if (quality.get(rid, {}).get("status") == "pass" or rid in template_support_ids)
        and (
            rid in template_support_ids
            or quality.get(rid, {}).get("active", "true").lower() == "true"
        )
        and bool(entry.get("gpr_rule") or entry.get("gpr_genes"))
    }
    annotation_evidence = {
        rid
        for rid, entry in mapped.items()
        if entry.get("sources")
        and quality.get(rid, {}).get("status") in {"pass", "rescue_for_growth"}
        and quality.get(rid, {}).get("active", "true").lower() == "true"
    }
    for rid in template_support_ids:
        if rid in universal.reactions:
            quality[rid] = {
                "status": "pass",
                "active": "true",
                "qc_status": "public_biomass_template_support",
            }
    for rid in template_medium_support_ids:
        if rid in universal.reactions:
            quality.setdefault(
                rid,
                {
                    "status": "pass",
                    "active": "true",
                    "qc_status": "medium_support",
                },
            )
    return quality, strict_evidence, annotation_evidence
