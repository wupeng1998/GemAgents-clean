"""Initial-model assembly for native reconstruction."""

from __future__ import annotations

from collections import defaultdict

from GemAgents.metabolic.evidence.mapping import metabolic_evidence_status
from GemAgents.metabolic.media.selection import metabolic_set_medium


def assemble_initial_model(
    universal,
    medium: object,
    strict_evidence: set[str],
    template_medium_support_ids: set[str],
    selected_biomass,
    biomass_id: str,
    biomass_product_drain_ids: set[str],
    reference_exchanges: dict,
    reference_prefix: str,
) -> tuple[object, set[str], dict[str, float]]:
    """Copy grounded reactions and propagate declared uptake to reference ports."""
    from cobra import Model as CobraModel

    selected_universal_medium = metabolic_set_medium(universal, medium)
    initial = CobraModel("native_v6_initial")
    keep = {rid for rid in strict_evidence if rid in universal.reactions}
    keep.update(rid for rid in template_medium_support_ids if rid in universal.reactions)
    keep.update(selected_universal_medium)
    keep.update(biomass_product_drain_ids)
    initial.add_reactions([universal.reactions.get_by_id(rid).copy() for rid in sorted(keep)])
    initial.add_reactions([selected_biomass.copy()])
    initial.objective = biomass_id
    selected_medium = metabolic_set_medium(initial, medium)

    # Reference exchanges are kept separate from v6 exchanges. Propagate the
    # selected uptake rate across shared BiGG/ModelSEED aliases so isolated
    # reference metabolites remain connected under minimal media.
    medium_aliases = defaultdict(set)
    for reaction_id, rate in selected_medium.items():
        reaction = initial.reactions.get_by_id(reaction_id)
        for metabolite in reaction.metabolites:
            names = {metabolite.id, metabolite.id.rsplit("_", 1)[0], metabolite.name}
            for namespace in ("bigg.metabolite", "seed.compound"):
                raw = metabolite.annotation.get(namespace, [])
                names.update(raw if isinstance(raw, list) else [raw])
            for name in names:
                if name:
                    medium_aliases[str(name)].add(float(rate))
    for source_id, details in reference_exchanges.items():
        reference_id = details.get("target_id", source_id)
        if reference_id not in initial.reactions:
            continue
        rates = set()
        for metabolite in initial.reactions.get_by_id(reference_id).metabolites:
            names = {
                metabolite.id,
                metabolite.id.removeprefix(reference_prefix),
                metabolite.id.rsplit("_", 1)[0],
                metabolite.name,
            }
            for namespace in ("bigg.metabolite", "seed.compound"):
                raw = metabolite.annotation.get(namespace, [])
                names.update(raw if isinstance(raw, list) else [raw])
            for name in names:
                rates.update(medium_aliases.get(str(name), set()))
        if len(rates) == 1:
            selected_medium[reference_id] = rates.pop()
    initial.medium = selected_medium
    return initial, selected_universal_medium, selected_medium


def close_transport_and_annotate(
    initial,
    universal,
    selected_medium: dict[str, float],
    template_medium_support_ids: set[str],
    mapped: dict,
) -> set[str]:
    """Add balanced nutrient transport and annotate initial-model evidence."""
    medium_bases = {
        next(iter(initial.reactions.get_by_id(rid).metabolites)).id.rsplit("_", 1)[0]
        for rid in selected_medium
        if rid in initial.reactions and len(initial.reactions.get_by_id(rid).metabolites) == 1
    }
    transport_support_ids = set()
    for reaction in universal.reactions:
        if not reaction.id.lower().endswith("tex") or reaction.boundary:
            continue
        bases = {met.id.rsplit("_", 1)[0] for met in reaction.metabolites}
        if bases & medium_bases and reaction.id not in initial.reactions:
            initial.add_reactions([reaction.copy()])
            transport_support_ids.add(reaction.id)
    template_medium_support_ids.update(transport_support_ids)
    for reaction in initial.reactions:
        if reaction.notes.get("reference_scaffold") == "true":
            continue
        entry = mapped.get(reaction.id)
        if entry:
            reaction.gene_reaction_rule = entry.get(
                "gpr_rule", " or ".join(sorted(set(entry.get("gpr_genes", []))))
            )
            reaction.notes["evidence_status"] = metabolic_evidence_status(entry)
        else:
            reaction.notes["evidence_status"] = (
                "medium_support"
                if reaction.id in template_medium_support_ids
                else "medium_or_biomass_template"
            )
    return transport_support_ids
