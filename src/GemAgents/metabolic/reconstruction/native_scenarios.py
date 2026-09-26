"""Native reconstruction growth-scenario and transport-closure setup."""

from __future__ import annotations


def prepare_native_growth_scenarios(
    *,
    initial,
    universal,
    reference_support_path,
    medium,
    minimum_growth: float,
    reference_support: bool,
    selected_medium: dict[str, float],
    template_medium_support_ids: set[str],
    mapped: dict,
    reference_scenarios_fn,
    oxygen_exchange_ids_fn,
    close_transport_fn,
    reference_growth_ceiling: bool = True,
) -> list[dict]:
    """Build declared/reference scenarios and close transport before LP setup."""
    reference_scenarios = (
        reference_scenarios_fn(reference_support_path, medium, minimum_growth)
        if reference_support
        else []
    )
    growth_scenarios = [
        {
            "name": "declared_growth",
            "minimum": minimum_growth,
            "medium": dict(selected_medium),
            "source": "user_declared_medium",
        }
    ]
    agent_oxygen = oxygen_exchange_ids_fn(initial) & set(selected_medium)
    for scenario in reference_scenarios:
        if scenario["name"] != "reference_supported_anaerobic_growth" or not agent_oxygen:
            continue
        growth_scenarios.append(
            {
                **scenario,
                "medium": {
                    reaction_id: rate
                    for reaction_id, rate in selected_medium.items()
                    if reaction_id not in agent_oxygen
                },
                "agent_oxygen_exchanges": sorted(agent_oxygen),
                "source": "public_reference_supported_condition",
                "evidence_role": "prior_input_not_external_validation",
                "reference_growth_ceiling": reference_growth_ceiling,
            }
        )
    close_transport_fn(
        initial,
        universal,
        selected_medium,
        template_medium_support_ids,
        mapped,
    )
    return growth_scenarios
