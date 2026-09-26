"""Fallback-tier and scenario-medium helpers for native gap filling."""

from __future__ import annotations

from GemAgents.metabolic.reconstruction.work_model import apply_forbidden_directions


def disposal_only_bounds(reaction) -> tuple[float, float]:
    """Restrict a boundary candidate to disposal-only directionality."""
    if len(reaction.metabolites) != 1:
        return (0.0, 0.0)
    coefficient = next(iter(reaction.metabolites.values()))
    if coefficient < 0:
        return max(0.0, reaction.lower_bound), max(0.0, reaction.upper_bound)
    return min(0.0, reaction.lower_bound), min(0.0, reaction.upper_bound)


def apply_scenario_medium(
    work,
    scenario_medium: dict[str, float],
    candidate_ids: set[str],
    universal,
    reference_candidate_ids: set[str],
    boundary_gapfill_ids: set[str],
) -> None:
    """Apply a scenario medium and restore controlled candidate bounds."""
    work.medium = scenario_medium
    for reaction_id in candidate_ids:
        if reaction_id in scenario_medium or reaction_id not in work.reactions:
            continue
        source_reaction = universal.reactions.get_by_id(reaction_id)
        if source_reaction.boundary and reaction_id in reference_candidate_ids:
            work.reactions.get_by_id(reaction_id).bounds = source_reaction.bounds
        elif reaction_id in boundary_gapfill_ids:
            work.reactions.get_by_id(reaction_id).bounds = disposal_only_bounds(source_reaction)


def open_chemistry_fallback(
    work,
    universal,
    template_bounds: dict[str, tuple[float, float]],
    costs: dict,
    forbidden_directions: set[tuple[str, str]],
) -> bool:
    """Open the chemistry-only tier and return whether it was newly opened."""
    if not template_bounds:
        return False
    work.add_reactions([universal.reactions.get_by_id(rid).copy() for rid in template_bounds])
    for rid, bounds in template_bounds.items():
        work.reactions.get_by_id(rid).bounds = bounds
        costs[work.reactions.get_by_id(rid).forward_variable] = 50.0
        costs[work.reactions.get_by_id(rid).reverse_variable] = 50.0
    apply_forbidden_directions(work, forbidden_directions)
    return True


def open_unverified_fallback(
    work,
    universal,
    unverified_bounds: dict[str, tuple[float, float]],
    costs: dict,
    mapped: dict,
    forbidden_directions: set[tuple[str, str]],
) -> bool:
    """Open the unverified chemistry tier and return whether it was opened."""
    if not unverified_bounds:
        return False
    work.add_reactions([universal.reactions.get_by_id(rid).copy() for rid in unverified_bounds])
    for rid, bounds in unverified_bounds.items():
        reaction = work.reactions.get_by_id(rid)
        reaction.bounds = bounds
        entry = mapped.get(rid, {})
        cost = (
            25.0
            if not entry.get("ambiguous") and (entry.get("gpr_rule") or entry.get("gpr_genes"))
            else 200.0
        )
        costs[reaction.forward_variable] = cost
        costs[reaction.reverse_variable] = cost
    apply_forbidden_directions(work, forbidden_directions)
    return True


def gapfill_stage(chemistry_open: bool, unverified_open: bool) -> str:
    """Return the auditable label for the currently enabled candidate tier."""
    if unverified_open:
        return "public_bigg_unverified_fallback"
    if chemistry_open:
        return "chemistry_fallback"
    return "annotation_supported"


def prepare_scenario(
    work,
    scenario: dict,
    candidate_ids: set[str],
    universal,
    reference_candidate_ids: set[str],
    boundary_gapfill_ids: set[str],
    costs: dict,
    additions_set: set[str],
) -> None:
    """Apply a scenario and zero the marginal cost of prior additions."""
    apply_scenario_medium(
        work,
        scenario["medium"],
        candidate_ids,
        universal,
        reference_candidate_ids,
        boundary_gapfill_ids,
    )
    scenario_costs = dict(costs)
    for reaction_id in additions_set:
        if reaction_id in work.reactions:
            reaction = work.reactions.get_by_id(reaction_id)
            scenario_costs[reaction.forward_variable] = 0.0
            scenario_costs[reaction.reverse_variable] = 0.0
    work.objective = work.problem.Objective(0, direction="min")
    work.objective.set_linear_coefficients(scenario_costs)
