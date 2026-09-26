"""Multi-condition weighted LP gap-fill solve loop."""

from __future__ import annotations

from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.reconstruction.fallbacks import (
    gapfill_stage,
    open_chemistry_fallback,
    open_unverified_fallback,
    prepare_scenario,
)
from GemAgents.solver_result import solve_with_classification as _solve_with_classification


def run_gapfill_scenarios(
    *,
    work,
    growth_scenarios: list[dict],
    candidate_ids: list[str],
    universal,
    reference_candidate_ids: set[str],
    boundary_gapfill_ids: set[str],
    costs: dict[str, float],
    additions_set: set[str],
    initial_ids: set[str],
    template_bounds: dict[str, tuple[float, float]],
    unverified_bounds: dict[str, tuple[float, float]],
    chemistry_open: bool,
    unverified_open: bool,
    forbidden_directions: set[tuple[str, str]],
    config: dict,
    out: Path,
    initial,
    biomass_id: str,
    selected_medium: dict[str, float],
    missing_biomass: list[str],
    biomass_report: dict,
    template: dict,
    rule_details: dict,
    reference_candidate_pool: int,
    template_gapfill_pool: int,
    unverified_gapfill_pool: int,
    quality: dict,
    mapped: dict,
    precursor_audit,
    write_json,
    solve_fn=None,
) -> dict:
    """Solve every required condition, opening fallback tiers only on failure."""
    solve_fn = solve_fn or _solve_with_classification
    solve_events = []
    scenario_results = []
    failed_error = None
    failed_scenario = None
    failed_status = None
    for scenario in growth_scenarios:
        transition = None
        while True:
            prepare_scenario(
                work,
                scenario,
                candidate_ids,
                universal,
                reference_candidate_ids,
                boundary_gapfill_ids,
                costs,
                additions_set,
            )
            reference_growth = scenario.get("reference_growth")
            if (
                reference_growth is not None
                and scenario.get("reference_growth_ceiling", True)
                and biomass_id in work.reactions
            ):
                biomass_reaction = work.reactions.get_by_id(biomass_id)
                biomass_reaction.upper_bound = min(
                    biomass_reaction.upper_bound, float(reference_growth)
                )
            chemistry_allowed = not chemistry_open and bool(template_bounds)
            unverified_allowed = (
                not unverified_open
                and bool(unverified_bounds)
                and config.get("allow_unverified_gapfill", False)
            )
            outcome = solve_fn(
                work, allow_candidate_expansion=chemistry_allowed or unverified_allowed
            )
            solve_events.append(
                {
                    **outcome.event(),
                    "scenario": scenario["name"],
                    "stage": gapfill_stage(chemistry_open, unverified_open),
                    "fallback_reason": transition,
                    "candidate_counts": {
                        "catalog": len(candidate_ids),
                        "present_reactions": len(work.reactions),
                        "enabled_candidates": sum(
                            rid in work.reactions and work.reactions.get_by_id(rid).bounds != (0, 0)
                            for rid in candidate_ids
                        ),
                    },
                }
            )
            write_json(out / "gapfill-solves.json", {"attempts": solve_events})
            if outcome.status == "optimal" or not outcome.candidate_pool_expansion_allowed:
                break
            transition = {"status": outcome.status, "attempt_id": outcome.attempt_id}
            if chemistry_allowed:
                chemistry_open = open_chemistry_fallback(
                    work,
                    universal,
                    template_bounds,
                    costs,
                    forbidden_directions,
                )
            elif unverified_allowed:
                unverified_open = open_unverified_fallback(
                    work,
                    universal,
                    unverified_bounds,
                    costs,
                    mapped,
                    forbidden_directions,
                )
            else:
                break
        if outcome.status != "optimal":
            failed_error = RuntimeError(outcome.error or outcome.status)
            failed_status = outcome.status
            failed_scenario = scenario["name"]
            break
        solution = outcome.solution
        active = {
            rid
            for rid in candidate_ids
            if rid not in initial_ids
            and rid in work.reactions
            and abs(float(solution.fluxes.get(rid, 0.0))) > work.tolerance / 10
        }
        new_additions = sorted(active - additions_set)
        additions_set.update(active)
        scenario_results.append(
            {
                **scenario,
                "stage": gapfill_stage(chemistry_open, unverified_open),
                "additions": new_additions,
            }
        )
    current_stage = gapfill_stage(chemistry_open, unverified_open)
    if failed_scenario is not None:
        precursor_report = {"status": "not_run", "reason": failed_status}
        if failed_status == "infeasible":
            precursor_report = precursor_audit(
                work,
                biomass_id,
                timeout=int(
                    config.get(
                        "gapfill_diagnostic_timeout",
                        min(int(config.get("qc_timeout", 30)), 10),
                    )
                ),
            )
        report = {
            "status": failed_status,
            "solver_status": failed_status,
            "error": str(failed_error),
            "solve_events": solve_events,
            "failed_scenario": failed_scenario,
            "candidate_pool": len(candidate_ids),
            "reference_candidate_pool": reference_candidate_pool,
            "template_gapfill_pool": template_gapfill_pool,
            "unverified_gapfill_pool": unverified_gapfill_pool,
            "biomass_id": biomass_id,
            "medium": selected_medium,
            "stage": current_stage,
            "growth_scenarios": scenario_results,
            "unmapped_biomass_metabolites": sorted(missing_biomass),
            "biomass_precursors": precursor_report,
        }
        write_json(out / "gapfill-report.json", report)
        if failed_status == "infeasible" and config.get("allow_non_growing_draft"):
            for reaction in initial.reactions:
                reaction.notes["gapfill"] = "false"
            write_json(
                out / "biomass-selection.json",
                {**biomass_report, "template": template, "gpr_resolution": rule_details},
            )
            return {"draft_model": initial, "draft_biomass": biomass_id}
        raise ToolError(
            f"v6 gap-filling could not satisfy required growth scenario {failed_scenario!r}"
        ) from failed_error
    return {
        "draft_model": None,
        "work": work,
        "additions_set": additions_set,
        "scenario_results": scenario_results,
        "gapfill_stage": current_stage,
        "solve_events": solve_events,
        "chemistry_open": chemistry_open,
        "unverified_open": unverified_open,
    }
