"""Final model assembly and post-gap-fill condition verification."""

from __future__ import annotations

import math

from GemAgents.metabolic.reconstruction.fallbacks import disposal_only_bounds
from GemAgents.metabolic.reconstruction.support import prune_condition_redundancy
from GemAgents.metabolic.reconstruction.work_model import apply_forbidden_directions


def finalize_gapfilled_model(
    initial,
    universal,
    biomass_id: str,
    additions_set: set[str],
    unverified_gapfill_ids: set[str],
    boundary_gapfill_ids: set[str],
    unverified_bounds: dict[str, tuple[float, float]],
    forbidden_directions: set[tuple[str, str]],
    scenario_results: list[dict],
    growth_scenarios: list[dict],
    selected_medium: dict[str, float],
    tolerance: float,
):
    """Apply solved additions, prune redundant condition-only reactions and verify growth."""
    additions = sorted(additions_set)
    model = initial.copy()
    model.tolerance = tolerance
    model.add_reactions([universal.reactions.get_by_id(rid).copy() for rid in additions])
    for reaction_id in additions_set & unverified_gapfill_ids:
        model.reactions.get_by_id(reaction_id).bounds = unverified_bounds[reaction_id]
    for reaction_id in additions_set & boundary_gapfill_ids:
        model.reactions.get_by_id(reaction_id).bounds = disposal_only_bounds(
            universal.reactions.get_by_id(reaction_id)
        )
    apply_forbidden_directions(model, forbidden_directions)
    model.objective = biomass_id
    condition_only_additions = sorted(
        {
            reaction_id
            for scenario in scenario_results
            if scenario["name"] != "declared_growth"
            for reaction_id in scenario["additions"]
        }
    )
    pruned_additions = prune_condition_redundancy(
        model, biomass_id, condition_only_additions, growth_scenarios, model.tolerance
    )
    additions = [reaction_id for reaction_id in additions if reaction_id not in pruned_additions]
    for scenario in scenario_results:
        scenario["additions"] = [
            reaction_id
            for reaction_id in scenario["additions"]
            if reaction_id not in pruned_additions
        ]
    verified_scenarios = []
    failed_verification = None
    for scenario in scenario_results:
        scenario_medium = {
            rid: value for rid, value in scenario["medium"].items() if rid in model.reactions
        }
        model.medium = scenario_medium
        original_biomass_bounds = model.reactions.get_by_id(biomass_id).bounds
        reference_growth = scenario.get("reference_growth")
        if reference_growth is not None and scenario.get("reference_growth_ceiling", True):
            biomass_reaction = model.reactions.get_by_id(biomass_id)
            biomass_reaction.upper_bound = min(
                biomass_reaction.upper_bound, float(reference_growth)
            )
        scenario_growth = model.slim_optimize(error_value=None)
        model.reactions.get_by_id(biomass_id).bounds = original_biomass_bounds
        verified = (
            scenario_growth is not None
            and math.isfinite(float(scenario_growth))
            and scenario_growth >= float(scenario["minimum"])
        )
        verified_scenarios.append(
            {
                **scenario,
                "growth": float(scenario_growth or 0.0),
                "verified": verified,
            }
        )
        if not verified and failed_verification is None:
            failed_verification = scenario["name"]
    model.medium = {rid: value for rid, value in selected_medium.items() if rid in model.reactions}
    growth = model.slim_optimize(error_value=None)
    return (
        model,
        additions,
        verified_scenarios,
        failed_verification,
        growth,
        pruned_additions,
    )
