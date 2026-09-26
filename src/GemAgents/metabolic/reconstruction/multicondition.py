"""Bounded shared-selection gap filling across declared conditions.

The implementation enumerates a small localized candidate set. It reports a
bounded-search result rather than claiming a global optimum when the cap is hit.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass

from GemAgents.metabolic.media.selection import metabolic_set_medium
from GemAgents.metabolic.reconstruction.local_gapfill import CandidateRecord


@dataclass(frozen=True)
class Condition:
    name: str
    medium: dict[str, float]
    biomass: str
    minimum_growth: float = 0.0
    maximum_growth: float | None = None


@dataclass(frozen=True)
class SharedSelectionResult:
    status: str
    selected_ids: tuple[str, ...] = ()
    objective_cost: float | None = None
    selection_variables: dict[str, int] | None = None
    condition_results: tuple[dict, ...] = ()
    candidate_count: int = 0
    localized_candidate_count: int = 0
    possible_leakage: str = "bounded_local_search"
    algorithm: str = "shared_selection_bounded_v1"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _evaluate(
    model,
    candidates: tuple[CandidateRecord, ...],
    subset: tuple[int, ...],
    condition: Condition,
):
    work = model.copy()
    existing = {reaction.id for reaction in work.reactions}
    additions = []
    for index in subset:
        reaction = candidates[index].reaction.copy()
        if reaction.id in existing:
            continue
        additions.append(reaction)
        existing.add(reaction.id)
    if additions:
        work.add_reactions(additions)
    metabolic_set_medium(work, condition.medium)
    work.objective = work.reactions.get_by_id(condition.biomass)
    solution = work.optimize(raise_error=False)
    backend_status = getattr(solution, "status", "internal_error")
    value = solution.objective_value if solution is not None else None
    if value is None:
        return False, {"name": condition.name, "status": "solver_failure", "maximum_growth": None}
    value = float(value)
    valid = backend_status == "optimal" and value >= condition.minimum_growth - work.tolerance
    if condition.maximum_growth is not None:
        valid &= value <= condition.maximum_growth + work.tolerance
    return valid, {
        "name": condition.name,
        "status": "pass" if valid else "fail",
        "maximum_growth": value,
        "backend_status": backend_status,
    }


def shared_selection(
    model,
    candidates: tuple[CandidateRecord, ...],
    conditions: tuple[Condition, ...],
    *,
    max_selected: int = 3,
    max_candidates: int = 16,
    rescue_budget: float = 0.0,
) -> SharedSelectionResult:
    if not conditions:
        raise ValueError("At least one declared condition is required")
    if max_selected < 0 or max_candidates < 1 or rescue_budget < 0:
        raise ValueError("Invalid shared-selection budget")
    localized = tuple(candidates[:max_candidates])
    if len(candidates) > max_candidates:
        return SharedSelectionResult(
            "SEARCH_INCOMPLETE",
            candidate_count=len(candidates),
            localized_candidate_count=len(localized),
        )
    for size in range(max_selected + 1):
        feasible: list[tuple[float, tuple[int, ...], tuple[dict, ...]]] = []
        for subset in itertools.combinations(range(len(localized)), size):
            cost = sum(localized[index].cost + localized[index].risk for index in subset)
            if cost > rescue_budget and any(
                localized[index].evidence_class == "authorized_rescue" for index in subset
            ):
                continue
            outcomes = tuple(
                _evaluate(model, localized, subset, condition)[1] for condition in conditions
            )
            if all(outcome["status"] == "pass" for outcome in outcomes):
                feasible.append((cost, subset, outcomes))
        if feasible:
            cost, subset, outcomes = min(feasible, key=lambda item: (item[0], item[1]))
            selected_ids = tuple(localized[index].reaction.id for index in subset)
            return SharedSelectionResult(
                "optimal_within_bounded_search",
                selected_ids,
                cost,
                {
                    candidate.reaction.id: int(index in subset)
                    for index, candidate in enumerate(localized)
                },
                outcomes,
                len(candidates),
                len(localized),
            )
    return SharedSelectionResult(
        "infeasible_under_shared_selection",
        candidate_count=len(candidates),
        localized_candidate_count=len(localized),
    )
