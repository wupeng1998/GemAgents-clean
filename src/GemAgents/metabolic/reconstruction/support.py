"""Deterministic reconstruction support and biomass boundary helpers."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.library.normalization import metabolic_equation_key
from GemAgents.metabolic.library.source_io import load_source_model
from GemAgents.metabolic.media.selection import metabolic_set_medium

try:
    from cobra.exceptions import Infeasible
except ImportError:  # pragma: no cover - cobra is a runtime dependency
    Infeasible = type("Infeasible", (Exception,), {})


def oxygen_exchange_ids(model) -> set[str]:
    """Identify molecular-oxygen uptake ports without assuming one namespace."""
    oxygen = set()
    for reaction in model.exchanges:
        if len(reaction.metabolites) != 1:
            continue
        metabolite = next(iter(reaction.metabolites))
        base_id = metabolite.id.rsplit("_", 1)[0].casefold()
        names = {metabolite.name.casefold()} if metabolite.name else set()
        for namespace in ("bigg.metabolite", "seed.compound"):
            raw = metabolite.annotation.get(namespace, [])
            values = raw if isinstance(raw, list) else [raw]
            names.update(str(value).casefold() for value in values)
        formula = str(metabolite.formula or "").replace(" ", "").casefold()
        if (
            base_id in {"o2", "cpd00007"}
            or names & {"o2", "oxygen", "dioxygen", "cpd00007"}
            or (formula == "o2" and metabolite.charge in {0, None})
        ):
            oxygen.add(reaction.id)
    return oxygen

def reference_growth_scenarios(
    source_path: Path, medium: object, minimum: float
) -> list[dict]:
    """Derive additional growth tasks only when the public template supports them."""
    if not source_path.is_file():
        return []
    reference = load_source_model(source_path)
    try:
        metabolic_set_medium(reference, medium)
    except ToolError:
        # A custom medium can use reconstruction-specific exchange IDs that
        # are not present in the public template.  The declared condition is
        # still enforced, but no unsupported phenotype is inferred.
        return []
    oxygen = oxygen_exchange_ids(reference) & set(reference.medium)
    if not oxygen:
        return []
    anaerobic_medium = {
        reaction_id: rate
        for reaction_id, rate in reference.medium.items()
        if reaction_id not in oxygen
    }
    reference.medium = anaerobic_medium
    try:
        growth = reference.slim_optimize(error_value=None)
    except Infeasible:
        return []
    if growth is None or not math.isfinite(float(growth)) or growth < minimum:
        return []
    return [
        {
            "name": "reference_supported_anaerobic_growth",
            "minimum": minimum,
            "reference_growth": float(growth),
            "reference_oxygen_exchanges": sorted(oxygen),
            "derivation": "remove molecular-oxygen uptake from the declared medium",
        }
    ]

def prune_condition_redundancy(
    model, biomass_id: str, additions: list[str], scenarios: list[dict], tolerance: float
) -> list[str]:
    """Greedily remove gap-fill additions that no required condition needs."""
    removed = []
    for reaction_id in sorted(additions):
        if reaction_id not in model.reactions:
            continue
        reaction = model.reactions.get_by_id(reaction_id)
        # A positive maintenance/forced-flux bound is itself part of the
        # biological task and must not be discarded merely because relaxing
        # it would make biomass easier to produce.
        if reaction.lower_bound > tolerance or reaction.upper_bound < -tolerance:
            continue
        with model:
            reaction.knock_out()
            valid = True
            for scenario in scenarios:
                model.medium = {
                    rid: rate for rid, rate in scenario["medium"].items() if rid in model.reactions
                }
                model.objective = biomass_id
                growth = model.slim_optimize(error_value=None)
                if (
                    growth is None
                    or not math.isfinite(float(growth))
                    or growth < float(scenario["minimum"]) - tolerance
                ):
                    valid = False
                    break
        if valid:
            model.remove_reactions([reaction_id], remove_orphans=True)
            removed.append(reaction_id)
    return removed

def add_biomass_product_drains(model, biomass) -> set[str]:
    """Drain explicit biomass pseudo-products so steady state permits growth."""
    drain_ids = set()
    for metabolite, coefficient in biomass.metabolites.items():
        label = f"{metabolite.id} {metabolite.name or ''}".casefold()
        if coefficient <= 0 or "biomass" not in label:
            continue
        existing = []
        # Template/reference normalization can replace a metabolite object
        # while preserving its public ID.  Search model boundaries by ID so
        # an existing DM_biomass_c is still recognized after that operation.
        for reaction in model.boundary:
            if not reaction.boundary or reaction is biomass:
                continue
            stoichiometry = next(
                (value for item, value in reaction.metabolites.items() if item.id == metabolite.id),
                0.0,
            )
            can_consume = (stoichiometry < 0 and reaction.upper_bound > 0) or (
                stoichiometry > 0 and reaction.lower_bound < 0
            )
            if can_consume:
                existing.append(reaction.id)
        if existing:
            drain_ids.update(existing)
            continue
        reaction_id = f"DM_{metabolite.id}"
        if reaction_id in model.reactions:
            raise ToolError(
                f"Biomass product {metabolite.id!r} has a non-disposal reaction "
                f"using reserved demand ID {reaction_id!r}"
            )
        drain = model.add_boundary(
            metabolite,
            type="demand",
            reaction_id=reaction_id,
            lb=0.0,
            ub=1000.0,
        )
        drain.notes["evidence_status"] = "biomass_product_drain"
        drain.annotation["biomass_template"] = biomass.annotation.get("biomass_template", "")
        drain_ids.add(drain.id)
    return drain_ids

def template_support_reaction_id(universal, support: dict, template_id: str) -> str:
    """Keep template-specific empirical equations separate from same-ID pools."""
    source_id = str(support["id"])
    if (
        support.get("support_role") != "biomass_precursor_assembly"
        or source_id not in universal.reactions
    ):
        return source_id
    source_key, _ = metabolic_equation_key(
        {
            metabolite_id: float(item["coefficient"])
            for metabolite_id, item in support.get("stoichiometry", {}).items()
        }
    )
    existing = universal.reactions.get_by_id(source_id)
    existing_key, _ = metabolic_equation_key(
        {
            metabolite.id: float(coefficient)
            for metabolite, coefficient in existing.metabolites.items()
        }
    )
    if source_key == existing_key:
        return source_id
    digest = hashlib.sha256(repr(source_key).encode("utf-8")).hexdigest()[:12]
    template = re.sub(r"[^A-Za-z0-9_]+", "_", template_id)
    return f"BIOMASSSUP_{template}_{source_id}_{digest}"
