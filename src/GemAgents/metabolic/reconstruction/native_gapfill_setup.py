"""Candidate-pool and LP work-model setup for native reconstruction."""

from __future__ import annotations

from GemAgents.metabolic.reconstruction.candidates import (
    build_gapfill_candidate_pools,
)
from GemAgents.metabolic.reconstruction.work_model import prepare_gapfill_work_model


def prepare_native_gapfill_setup(
    *,
    universal,
    mapped: dict,
    quality: dict,
    strict_evidence: set[str],
    annotation_evidence: set[str],
    reference_candidate_ids: set[str],
    reference_candidate_meta: dict[str, dict],
    biomass_id: str,
    biomass,
    biomass_product_drain_ids: set[str],
    selected_universal_medium: set[str],
    selected_medium: dict[str, float],
    initial,
    minimum_growth: float,
    forbidden_directions: set[str],
    config: dict,
) -> dict:
    """Prepare grounded candidates and the isolated weighted-LP work model."""
    candidate_pools = build_gapfill_candidate_pools(
        universal,
        mapped,
        quality,
        strict_evidence,
        annotation_evidence,
        reference_candidate_ids,
        biomass_id,
        selected_universal_medium,
        config,
    )
    candidate_ids = candidate_pools["candidate_ids"]
    template_gapfill_ids = candidate_pools["template_gapfill_ids"]
    initial_ids = set(initial.reactions.list_attr("id"))
    work, costs = prepare_gapfill_work_model(
        universal,
        biomass,
        biomass_id,
        biomass_product_drain_ids,
        candidate_ids,
        candidate_pools["template_bounds"],
        candidate_pools["unverified_bounds"],
        selected_medium,
        reference_candidate_ids,
        reference_candidate_meta,
        quality,
        template_gapfill_ids,
        initial_ids,
        minimum_growth,
        forbidden_directions,
    )
    return {
        **candidate_pools,
        "work": work,
        "costs": costs,
        "initial_ids": initial_ids,
        "forbidden_directions": forbidden_directions,
    }
