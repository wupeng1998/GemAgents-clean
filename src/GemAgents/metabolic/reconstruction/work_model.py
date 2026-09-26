"""LP work-model preparation for evidence-aware native gap filling."""

from __future__ import annotations


def apply_forbidden_directions(work, forbidden_directions: set[tuple[str, str]]) -> None:
    """Apply caller-authorized directional restrictions to a work model."""
    for reaction_id, sign in forbidden_directions:
        if reaction_id not in work.reactions:
            continue
        reaction = work.reactions.get_by_id(reaction_id)
        reaction.bounds = (
            (reaction.lower_bound, min(0.0, reaction.upper_bound))
            if sign == "+"
            else (max(0.0, reaction.lower_bound), reaction.upper_bound)
        )


def prepare_gapfill_work_model(
    universal,
    biomass,
    biomass_id: str,
    biomass_product_drain_ids: set[str],
    candidate_ids: set[str],
    template_bounds: dict[str, tuple[float, float]],
    unverified_bounds: dict[str, tuple[float, float]],
    selected_medium: dict[str, float],
    reference_candidate_ids: set[str],
    reference_candidate_meta: dict,
    quality: dict,
    template_gapfill_ids: set[str],
    initial_ids: set[str],
    minimum_growth: float,
    forbidden_directions: set[tuple[str, str]],
):
    """Prepare the constrained LP model and reaction cost coefficients."""
    # Template and unverified reactions are deliberately delayed until their
    # fallback tier is opened.  The old implementation copied the whole
    # universe and then called ``Model.remove_reactions`` once per reaction.
    # COBRApy removes two optlang variables for every reaction and reindexes
    # the remaining container after each removal, which is quadratic for the
    # v6 library (and eventually triggers optlang Container reindex errors).
    # Build the initial work model from the reactions that are available in
    # the first tier instead; the fallback functions still copy the delayed
    # reactions from ``universal`` when they are opened.
    deferred_ids = template_bounds.keys() | unverified_bounds.keys()
    biomass_like = {
        reaction.id
        for reaction in universal.reactions
        if (
            reaction.id not in biomass_product_drain_ids
            and (
                reaction.id.lower() in {"growth", "biomass"}
                or "biomass" in reaction.id.lower()
            )
        )
    }
    keep_ids = {
        reaction.id
        for reaction in universal.reactions
        if reaction.id not in deferred_ids and reaction.id not in biomass_like
    }
    from cobra import Model as CobraModel

    work = CobraModel(f"{universal.id}_gapfill")
    # Keep the solver backend and timeout selected by the validated universe;
    # a fresh COBRA model otherwise inherits the process default (often CPLEX)
    # instead of the native GLPK backend.
    solver_interface = getattr(universal.solver, "interface", None)
    if solver_interface is not None:
        work.solver = solver_interface
    solver_timeout = getattr(universal.solver.configuration, "timeout", None)
    if solver_timeout is not None:
        work.solver.configuration.timeout = solver_timeout
    work.add_reactions(
        [universal.reactions.get_by_id(rid).copy() for rid in sorted(keep_ids)]
    )
    work.add_reactions([biomass.copy()])
    work.objective = biomass_id
    # GLPK's feasibility tolerance is 1e-7 by default.  Using a tighter
    # model-level threshold here classified otherwise optimal v6 LPs with
    # 2e-8 residuals as numerical failures.  Keep the gap-fill check aligned
    # with the backend while still rejecting materially infeasible solutions.
    work.tolerance = 1e-7
    work.medium = selected_medium
    for reaction_id in candidate_ids:
        if reaction_id in selected_medium or reaction_id not in work.reactions:
            continue
        source_reaction = universal.reactions.get_by_id(reaction_id)
        reaction = work.reactions.get_by_id(reaction_id)
        if source_reaction.boundary and reaction_id in reference_candidate_ids:
            reaction.bounds = source_reaction.bounds
    work.reactions.get_by_id(biomass_id).lower_bound = minimum_growth
    costs = {}
    for reaction in work.reactions:
        if reaction.id not in candidate_ids and reaction.id not in initial_ids:
            reaction.bounds = (0, 0)
        status = quality.get(reaction.id, {}).get("status")
        cost = (
            3.0
            if reaction.id in reference_candidate_ids
            and reference_candidate_meta.get(reaction.id, {}).get("gpr")
            and reaction.id not in initial_ids
            else 20.0
            if reaction.id in reference_candidate_ids and reaction.id not in initial_ids
            else 50.0
            if reaction.id in template_gapfill_ids and reaction.id not in initial_ids
            else 5.0
            if status == "rescue_for_growth" and reaction.id not in initial_ids
            else 1.0
            if reaction.id in candidate_ids and reaction.id not in initial_ids
            else 0.0
        )
        costs[reaction.forward_variable] = cost
        costs[reaction.reverse_variable] = cost
    apply_forbidden_directions(work, forbidden_directions)
    work.objective = work.problem.Objective(0, direction="min")
    work.objective.set_linear_coefficients(costs)
    return work, costs
