"""Named QC scopes and carrier coverage registry."""

from __future__ import annotations

DEFAULT_SCOPE_NAMES = (
    "sbml_structure",
    "chemical_information",
    "stoichiometric_consistency",
    "strict_closed_material",
    "energy_carriers",
    "no_uptake_with_excretion",
    "biomass_joint_reachability",
    "medium_phenotype",
    "gpr_evidence",
    "provenance",
    "export_consistency",
)

CARRIER_REGISTRY = {
    "ATP": (("atp", "adp"), ("cpd00002", "cpd00008")),
    "GTP": (("gtp", "gdp"),),
    "NADH": (("nadh", "nad"),),
    "NADPH": (("nadph", "nadp"),),
}


def _base(identifier: str) -> str:
    return identifier.rsplit("_", 1)[0].casefold()


def carrier_coverage(model) -> dict[str, object]:
    by_compartment = {}
    for metabolite in model.metabolites:
        by_compartment.setdefault(metabolite.compartment, set()).add(_base(metabolite.id))
    carriers = {}
    for name, alternatives in CARRIER_REGISTRY.items():
        covered = []
        for compartment, identifiers in by_compartment.items():
            if any({high, low} <= identifiers for high, low in alternatives):
                covered.append(compartment)
        carriers[name] = {"covered_compartments": sorted(covered), "covered": bool(covered)}
    return {
        "carriers": carriers,
        "covered": sum(item["covered"] for item in carriers.values()),
        "total": len(carriers),
    }
