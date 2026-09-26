"""Native reconstruction orchestration over the deterministic leaf modules."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from GemAgents.metabolic.contracts import ReconstructionContext


def metabolic_native_build_model(
    universal,
    universe_path: Path,
    mapped: dict,
    evidence: list[dict],
    config: dict,
    out: Path,
    *,
    context: ReconstructionContext | None = None,
    choose_biomass_fn: Callable[..., tuple[dict, dict]],
    add_reference_support_fn: Callable[..., tuple[set[str], dict, dict, dict]],
    add_biomass_product_drains_fn: Callable[..., set[str]],
    fasta_fn: Callable[..., list[tuple[str, str]]],
    write_json_fn: Callable[..., None],
    reference_growth_scenarios_fn: Callable[..., list[dict]],
    oxygen_exchange_ids_fn: Callable[..., set[str]],
    close_transport_fn: Callable[..., Any],
    biomass_precursor_audit_fn: Callable[..., dict],
    evidence_status_fn: Callable[..., str],
    solve_fn: Callable[..., Any],
):
    """Build and LP-gap-fill a model directly from the public v6 inputs.

    The leaf modules own deterministic work; callbacks keep the historical
    facade's compatibility seams (and test injection points) without making
    this layer import ``legacy`` or the tool facade.
    """
    catalog_path = Path(config.get("biomass_library", "")) / "catalog.json"
    from GemAgents.metabolic.reconstruction.native_context import (
        prepare_native_reconstruction_context,
    )

    native_context = prepare_native_reconstruction_context(
        catalog_path=catalog_path,
        config=config,
        input_path=Path(config["input"]),
        universal=universal,
        mapped=mapped,
        evidence=evidence,
        out=out,
        choose_biomass_fn=choose_biomass_fn,
        add_reference_support_fn=add_reference_support_fn,
        add_biomass_product_drains_fn=add_biomass_product_drains_fn,
        fasta_fn=fasta_fn,
        write_json_fn=write_json_fn,
    )
    template = native_context["template"]
    biomass_report = native_context["biomass_report"]
    biomass_id = native_context["biomass_id"]
    template_support_ids = native_context["template_support_ids"]
    template_medium_support_ids = native_context["template_medium_support_ids"]
    biomass = native_context["biomass"]
    missing_biomass = native_context["missing_biomass"]
    rule_details = native_context["rule_details"]
    reference_support_path = native_context["reference_support_path"]
    reference_prefix = native_context["reference_prefix"]
    reference_candidate_ids = native_context["reference_candidate_ids"]
    reference_candidate_meta = native_context["reference_candidate_meta"]
    reference_exchanges = native_context["reference_exchanges"]
    biomass_product_drain_ids = native_context["biomass_product_drain_ids"]

    from GemAgents.metabolic.reconstruction.native_setup import (
        prepare_native_initial_setup,
    )

    native_setup = prepare_native_initial_setup(
        universal=universal,
        mapped=mapped,
        config=config,
        reaction_library_path=Path(config["reaction_library"]),
        reference_candidate_meta=reference_candidate_meta,
        template_support_ids=template_support_ids,
        template_medium_support_ids=template_medium_support_ids,
        biomass=biomass,
        biomass_id=biomass_id,
        biomass_product_drain_ids=biomass_product_drain_ids,
        reference_exchanges=reference_exchanges,
        reference_prefix=reference_prefix,
        reference_support_path=reference_support_path,
        reference_scenarios_fn=reference_growth_scenarios_fn,
        oxygen_exchange_ids_fn=oxygen_exchange_ids_fn,
        close_transport_fn=close_transport_fn,
        out=out,
        reference_candidate_ids=reference_candidate_ids,
    )
    quality = native_setup["quality"]
    strict_evidence = native_setup["strict_evidence"]
    annotation_evidence = native_setup["annotation_evidence"]
    initial = native_setup["initial"]
    selected_universal_medium = native_setup["selected_universal_medium"]
    selected_medium = native_setup["selected_medium"]
    minimum_growth = native_setup["minimum_growth"]
    growth_scenarios = native_setup["growth_scenarios"]
    forbidden_directions = set((context or ReconstructionContext()).forbidden_directions)

    # A reference scaffold is an explicitly reference-assisted carry-forward
    # track. Running the minimum-cost gap-fill LP would discard the scaffold
    # reactions that this track is intended to preserve.
    if config.get("reference_scaffold"):
        import json

        initial.notes["growth_scenarios"] = json.dumps(
            [
                {
                    "name": scenario["name"],
                    "minimum": scenario["minimum"],
                    "medium": scenario["medium"],
                    "source": scenario["source"],
                    "evidence_role": scenario.get("evidence_role", "declared_input"),
                    "reference_growth": scenario.get("reference_growth"),
                    "reference_growth_ceiling": scenario.get("reference_growth_ceiling", True),
                }
                for scenario in growth_scenarios
            ],
            sort_keys=True,
        )
        growth = initial.slim_optimize(error_value=None)
        write_json_fn(
            out / "gapfill-report.json",
            {
                "status": "reference_scaffold",
                "algorithm": "reference-assisted scaffold carry-forward",
                "growth": float(growth or 0.0),
                "medium": selected_medium,
                "growth_scenarios": growth_scenarios,
                "candidate_pool": len(reference_candidate_ids),
                "reference_candidate_pool": len(reference_candidate_ids),
                "additions": [],
                "reference_scaffold": True,
            },
        )
        return initial, biomass_id

    from GemAgents.metabolic.reconstruction.native_gapfill_setup import (
        prepare_native_gapfill_setup,
    )

    gapfill_setup = prepare_native_gapfill_setup(
        universal=universal,
        mapped=mapped,
        quality=quality,
        strict_evidence=strict_evidence,
        annotation_evidence=annotation_evidence,
        reference_candidate_ids=reference_candidate_ids,
        reference_candidate_meta=reference_candidate_meta,
        biomass_id=biomass_id,
        biomass=biomass,
        biomass_product_drain_ids=biomass_product_drain_ids,
        selected_universal_medium=selected_universal_medium,
        selected_medium=selected_medium,
        initial=initial,
        minimum_growth=minimum_growth,
        forbidden_directions=forbidden_directions,
        config=config,
    )
    candidate_ids = gapfill_setup["candidate_ids"]
    template_gapfill_ids = gapfill_setup["template_gapfill_ids"]
    unverified_gapfill_ids = gapfill_setup["unverified_gapfill_ids"]
    boundary_gapfill_ids = gapfill_setup["boundary_gapfill_ids"]
    template_bounds = gapfill_setup["template_bounds"]
    unverified_bounds = gapfill_setup["unverified_bounds"]
    initial_ids = gapfill_setup["initial_ids"]
    work = gapfill_setup["work"]
    costs = gapfill_setup["costs"]

    from GemAgents.metabolic.reconstruction.gapfill_loop import run_gapfill_scenarios

    loop = run_gapfill_scenarios(
        work=work,
        growth_scenarios=growth_scenarios,
        candidate_ids=candidate_ids,
        universal=universal,
        reference_candidate_ids=reference_candidate_ids,
        boundary_gapfill_ids=boundary_gapfill_ids,
        costs=costs,
        additions_set=set(),
        initial_ids=initial_ids,
        template_bounds=template_bounds,
        unverified_bounds=unverified_bounds,
        chemistry_open=False,
        unverified_open=False,
        forbidden_directions=forbidden_directions,
        config=config,
        out=out,
        initial=initial,
        biomass_id=biomass_id,
        selected_medium=selected_medium,
        missing_biomass=missing_biomass,
        biomass_report=biomass_report,
        template=template,
        rule_details=rule_details,
        reference_candidate_pool=len(reference_candidate_ids),
        template_gapfill_pool=len(template_gapfill_ids),
        unverified_gapfill_pool=len(unverified_gapfill_ids),
        quality=quality,
        mapped=mapped,
        precursor_audit=biomass_precursor_audit_fn,
        write_json=write_json_fn,
        solve_fn=solve_fn,
    )
    if loop.get("draft_model") is not None:
        return loop["draft_model"], loop["draft_biomass"]
    work = loop["work"]
    additions_set = loop["additions_set"]
    scenario_results = loop["scenario_results"]
    gapfill_stage = loop["gapfill_stage"]
    solve_events = loop["solve_events"]

    from GemAgents.metabolic.reconstruction.native_gapfill_finalize import (
        finalize_native_gapfill_result,
    )

    return finalize_native_gapfill_result(
        initial=initial,
        universal=universal,
        biomass_id=biomass_id,
        additions_set=additions_set,
        unverified_gapfill_ids=unverified_gapfill_ids,
        boundary_gapfill_ids=boundary_gapfill_ids,
        unverified_bounds=unverified_bounds,
        forbidden_directions=forbidden_directions,
        scenario_results=scenario_results,
        growth_scenarios=growth_scenarios,
        selected_medium=selected_medium,
        tolerance=work.tolerance,
        allow_non_growing_draft=bool(config.get("allow_non_growing_draft")),
        gapfill_stage=gapfill_stage,
        candidate_ids=candidate_ids,
        reference_candidate_ids=reference_candidate_ids,
        template_gapfill_ids=template_gapfill_ids,
        reference_candidate_meta=reference_candidate_meta,
        mapped=mapped,
        quality=quality,
        biomass_report=biomass_report,
        template=template,
        rule_details=rule_details,
        solve_events=solve_events,
        out=out,
        evidence_status_fn=evidence_status_fn,
        write_json_fn=write_json_fn,
    )
