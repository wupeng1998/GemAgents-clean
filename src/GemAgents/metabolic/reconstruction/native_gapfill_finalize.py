"""Finalize native gap-fill solutions and persist their reports."""

from __future__ import annotations

from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.reconstruction.finalize import finalize_gapfilled_model
from GemAgents.metabolic.reconstruction.gapfill_report import finalize_gapfill_report


def finalize_native_gapfill_result(
    *,
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
    allow_non_growing_draft: bool,
    gapfill_stage: str,
    candidate_ids: list[str],
    reference_candidate_ids: set[str],
    template_gapfill_ids: set[str],
    reference_candidate_meta: dict,
    mapped: dict,
    quality: dict,
    biomass_report: dict,
    template: dict,
    rule_details: dict,
    solve_events: list[dict],
    out: Path,
    evidence_status_fn,
    write_json_fn,
) -> tuple[object, str]:
    """Apply solved additions, enforce growth policy, and write final reports."""
    (
        model,
        additions,
        verified_scenarios,
        failed_verification,
        growth,
        pruned_additions,
    ) = finalize_gapfilled_model(
        initial,
        universal,
        biomass_id,
        additions_set,
        unverified_gapfill_ids,
        boundary_gapfill_ids,
        unverified_bounds,
        forbidden_directions,
        scenario_results,
        growth_scenarios,
        selected_medium,
        tolerance,
    )
    if failed_verification is not None:
        if allow_non_growing_draft:
            write_json_fn(
                out / "gapfill-report.json",
                {
                    "status": "non_growing_draft",
                    "algorithm": "v6 weighted LP gap-fill",
                    "stage": gapfill_stage,
                    "candidate_pool": len(candidate_ids),
                    "reference_candidate_pool": len(reference_candidate_ids),
                    "additions": additions,
                    "growth": float(growth or 0.0),
                    "medium": selected_medium,
                    "growth_scenarios": verified_scenarios,
                    "reason": f"required growth scenario failed: {failed_verification}",
                },
            )
            return model, biomass_id
        raise ToolError(f"Gap-fill LP returned a model that fails scenario {failed_verification!r}")
    finalize_gapfill_report(
        model=model,
        additions=additions,
        verified_scenarios=verified_scenarios,
        growth=growth,
        pruned_additions=pruned_additions,
        unverified_gapfill_ids=unverified_gapfill_ids,
        boundary_gapfill_ids=boundary_gapfill_ids,
        template_gapfill_ids=template_gapfill_ids,
        reference_candidate_meta=reference_candidate_meta,
        mapped=mapped,
        quality=quality,
        biomass_report=biomass_report,
        template=template,
        rule_details=rule_details,
        gapfill_stage=gapfill_stage,
        solve_events=solve_events,
        candidate_ids=candidate_ids,
        reference_candidate_ids=reference_candidate_ids,
        selected_medium=selected_medium,
        forbidden_directions=forbidden_directions,
        out=out,
        evidence_status=evidence_status_fn,
        write_json=write_json_fn,
    )
    return model, biomass_id
