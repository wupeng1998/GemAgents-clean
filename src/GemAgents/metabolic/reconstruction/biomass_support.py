"""Biomass-template support reaction assembly for native reconstruction."""

from __future__ import annotations

import json

from GemAgents.metabolic.evidence import filter_source_records
from GemAgents.metabolic.reconstruction.support import template_support_reaction_id


def build_biomass_reaction(universal, biomass_data: dict, template: dict):
    """Build the selected public biomass reaction and retain unresolved terms."""
    from cobra import Metabolite, Reaction

    biomass_id = biomass_data["id"]
    biomass = Reaction(biomass_id, name=biomass_data.get("name", biomass_id))
    source_stoich = biomass_data.get("source_stoichiometry", {})
    source_to_canonical = biomass_data.get("source_to_canonical", {})
    missing_biomass = []
    for source_id, item in source_stoich.items():
        canonical_id = source_to_canonical.get(source_id, source_id)
        # Keep unresolved public biomass terms explicit in the model instead
        # of silently deleting them.  They are catalog-only until v6 obtains
        # a verified chemistry mapping and are reported as such.
        if canonical_id not in universal.metabolites:
            metabolite = Metabolite(
                canonical_id,
                name=canonical_id,
                compartment=item.get("compartment", "c"),
                formula=item.get("formula"),
                charge=item.get("charge"),
            )
            metabolite.annotation["biomass_catalog_only"] = "true"
            universal.add_metabolites([metabolite])
            missing_biomass.append(canonical_id)
        biomass_data["stoichiometry"][canonical_id] = float(item["coefficient"])
    biomass.add_metabolites(
        {
            universal.metabolites.get_by_id(mid): coefficient
            for mid, coefficient in biomass_data["stoichiometry"].items()
        }
    )
    biomass.bounds = (0, 1000)
    biomass.annotation["biomass_template"] = template["id"]
    biomass.annotation["source_biomass_id"] = biomass_data.get("source_id", biomass_id)
    biomass.notes["unmapped_source_metabolites"] = ",".join(sorted(missing_biomass))
    return biomass, missing_biomass, source_to_canonical


def remap_biomass_precursors(
    biomass,
    universal,
    source_to_canonical: dict,
    reference_met_map: dict,
    biomass_report: dict,
) -> None:
    """Reconnect biomass terms to public reference metabolites when available."""
    for source_id, canonical_id in source_to_canonical.items():
        target = (
            universal.metabolites.get_by_id(source_id)
            if source_id in universal.metabolites
            else None
        )
        canonical_met = next((met for met in biomass.metabolites if met.id == canonical_id), None)
        if target is None or target.id == canonical_id or canonical_met is None:
            continue
        coefficient = biomass.metabolites[canonical_met]
        biomass.subtract_metabolites({canonical_met: coefficient})
        biomass.add_metabolites({target: coefficient})
        biomass_report.setdefault("reference_biomass_precursor_remaps", []).append(
            {"source": source_id, "from": canonical_id, "to": target.id}
        )


def add_template_support(universal, template: dict, biomass_report: dict):
    """Add and validate the selected template's support reaction records."""
    from cobra import Metabolite, Reaction

    # public template.  Missing metabolites are copied with their
    # public formula/charge and are still auditable through the template
    # provenance fields.
    template_support_ids = set()
    template_medium_support_ids = set()
    template_support_excluded = []
    support_catalog_ids = {str(item.get("id")) for item in template.get("support_reactions", [])}
    for support in template.get("support_reactions", []):
        support, excluded = filter_source_records(support)
        biomass_report.setdefault("quarantined_sources", []).extend(excluded)
        if support is None:
            continue
        source_reaction_id = str(support["id"])
        reaction_id = template_support_reaction_id(universal, support, str(template["id"]))
        support_catalog_ids.add(reaction_id)
        if reaction_id != source_reaction_id:
            biomass_report.setdefault("template_specific_support_variants", []).append(
                {
                    "source_reaction_id": source_reaction_id,
                    "model_reaction_id": reaction_id,
                }
            )
        empirical_assembly = support.get("support_role") == "biomass_precursor_assembly"
        if reaction_id in universal.reactions:
            if empirical_assembly:
                existing = universal.reactions.get_by_id(reaction_id)
                existing.notes["evidence_status"] = "public_biomass_precursor_assembly"
                existing.annotation["biomass_support"] = "biomass_precursor_assembly"
                if support.get("source_gene_reaction_rule"):
                    existing.notes["source_gene_reaction_rule"] = support[
                        "source_gene_reaction_rule"
                    ]
                template_support_ids.add(reaction_id)
                continue
            # A v6 reaction may carry the same BiGG identifier but a
            # protonation state that failed QC.  When the public reference
            # record is balanced, use its stoichiometry and chemical fields
            # for this template-local copy.  The change is auditable and is
            # restricted to the small biomass-support set.
            existing = universal.reactions.get_by_id(reaction_id)
            old_metabolites = dict(existing.metabolites)
            old_properties = {}
            baseline_bad = set()
            for candidate in universal.reactions:
                try:
                    if candidate.check_mass_balance():
                        baseline_bad.add(candidate.id)
                except (TypeError, ValueError):
                    pass
            for metabolite_id, item in support.get("stoichiometry", {}).items():
                if metabolite_id in universal.metabolites:
                    metabolite = universal.metabolites.get_by_id(metabolite_id)
                    old_properties[metabolite_id] = (metabolite.formula, metabolite.charge)
                    if item.get("formula"):
                        metabolite.formula = item["formula"]
                    if item.get("charge") is not None:
                        metabolite.charge = item["charge"]
                else:
                    metabolite = Metabolite(
                        metabolite_id,
                        name=metabolite_id,
                        compartment=item.get("compartment", "c"),
                        formula=item.get("formula"),
                        charge=item.get("charge"),
                    )
                    metabolite.annotation["biomass_support_catalog"] = support.get(
                        "support_role", "template_support"
                    )
                    universal.add_metabolites([metabolite])
            # Clear stoichiometry through COBRApy's public API so solver
            # coefficients and metabolite reverse references stay in sync.
            existing.subtract_metabolites(dict(existing.metabolites))
            existing.add_metabolites(
                {
                    universal.metabolites.get_by_id(mid): float(item["coefficient"])
                    for mid, item in support.get("stoichiometry", {}).items()
                }
            )
            try:
                existing_balance = existing.check_mass_balance()
            except (TypeError, ValueError):
                existing_balance = {"error": "uncheckable"}
            newly_imbalanced = set()
            for candidate in universal.reactions:
                if candidate.id in support_catalog_ids:
                    continue
                try:
                    if candidate.check_mass_balance() and candidate.id not in baseline_bad:
                        newly_imbalanced.add(candidate.id)
                except (TypeError, ValueError):
                    pass
            if newly_imbalanced:
                for metabolite_id, (formula, charge) in old_properties.items():
                    metabolite = universal.metabolites.get_by_id(metabolite_id)
                    metabolite.formula, metabolite.charge = formula, charge
                existing.subtract_metabolites(dict(existing.metabolites))
                existing.add_metabolites(old_metabolites)
                existing_balance = {"newly_imbalanced": sorted(newly_imbalanced)}
            if existing_balance:
                template_support_excluded.append(
                    {"reaction": reaction_id, "balance_residual": existing_balance}
                )
            else:
                template_support_ids.add(reaction_id)
            continue
        reaction = Reaction(
            reaction_id,
            name=support.get("name", reaction_id),
            lower_bound=float(support.get("bounds", [0.0, 1000.0])[0]),
            upper_bound=float(support.get("bounds", [0.0, 1000.0])[1]),
        )
        stoichiometry = {}
        for metabolite_id, item in support.get("stoichiometry", {}).items():
            if metabolite_id in universal.metabolites:
                metabolite = universal.metabolites.get_by_id(metabolite_id)
            else:
                metabolite = Metabolite(
                    metabolite_id,
                    name=metabolite_id,
                    compartment=item.get("compartment", "c"),
                    formula=item.get("formula"),
                    charge=item.get("charge"),
                )
                metabolite.annotation["biomass_support_catalog"] = support.get(
                    "support_role", "template_support"
                )
                universal.add_metabolites([metabolite])
            stoichiometry[metabolite] = float(item["coefficient"])
        reaction.add_metabolites(stoichiometry)
        reaction.annotation["biomass_support"] = support.get("support_role", "template_support")
        reaction.annotation["source_reaction_id"] = support.get(
            "source_reaction_id", source_reaction_id
        )
        reaction.notes["evidence_status"] = "public_biomass_template_support"
        alternate_sources = support.get("alternate_sources", [])
        if alternate_sources:
            reaction.notes["alternate_support_sources"] = json.dumps(
                alternate_sources, ensure_ascii=False, sort_keys=True
            )
        try:
            support_balance = reaction.check_mass_balance()
        except (TypeError, ValueError):
            support_balance = {"error": "uncheckable"}
        if support_balance and not empirical_assembly:
            template_support_excluded.append(
                {"reaction": reaction_id, "balance_residual": support_balance}
            )
            continue
        if empirical_assembly:
            reaction.notes["balance_status"] = "empirical_biomass_pseudoreaction"
            if support.get("source_gene_reaction_rule"):
                reaction.notes["source_gene_reaction_rule"] = support[
                    "source_gene_reaction_rule"
                ]
        universal.add_reactions([reaction])
        template_support_ids.add(reaction_id)
    # Balanced ModelSEED equivalents retain the reference BiGG GPR while
    # avoiding chemically rejected source equations.
    support_aliases = []
    for original_alias in template.get("support_aliases", []):
        alias, excluded = filter_source_records(original_alias)
        biomass_report.setdefault("quarantined_sources", []).extend(excluded)
        if alias is not None:
            support_aliases.append(alias)
    for alias in support_aliases:
        canonical_id = alias.get("canonical_reaction_id")
        if canonical_id in universal.reactions:
            template_support_ids.add(canonical_id)
            biomass_report.setdefault("template_support_aliases", []).append(
                {
                    "source_reaction_id": alias.get("source_reaction_id", ""),
                    "canonical_reaction_id": canonical_id,
                    "role": alias.get("role", "balanced_modelseed_equivalent"),
                }
            )
    for support in template.get("medium_support_reactions", []):
        reaction_id = support.get("id")
        if reaction_id in universal.reactions:
            template_medium_support_ids.add(reaction_id)
            biomass_report.setdefault("medium_support_reactions", []).append(
                {"id": reaction_id, "role": support.get("role", "medium_support")}
            )
    biomass_report["template_support_reactions"] = sorted(template_support_ids)
    biomass_report["template_support_excluded"] = template_support_excluded

    return (
        template_support_ids,
        template_medium_support_ids,
        template_support_excluded,
        support_aliases,
    )
