"""Experimental local multi-cut repair search.

The search is intentionally local and budgeted; it never claims a global optimum.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass

Direction = tuple[str, str]


@dataclass(frozen=True)
class SearchResult:
    status: str
    edits: tuple[Direction, ...] = ()
    cost: float = 0.0
    covered_witnesses: int = 0
    nodes: int = 0
    algorithm: str = "local_hitting_set_v1"


def witness_directions(
    witnesses: Iterable[dict[str, float]], protected: Iterable[str] = ()
) -> tuple[frozenset[Direction], ...]:
    protected = set(protected)
    return tuple(
        frozenset(
            (reaction_id, "+" if flux > 0 else "-")
            for reaction_id, flux in witness.items()
            if flux and reaction_id not in protected
        )
        for witness in witnesses
    )


def select_multicut(
    witnesses: Iterable[dict[str, float]],
    *,
    costs: dict[str, float] | None = None,
    protected: Iterable[str] = (),
    max_edits: int = 2,
    max_nodes: int = 10_000,
    evaluator: Callable[[tuple[Direction, ...]], bool] | None = None,
) -> SearchResult:
    """Find a low-cost direction hitting every witness within a finite budget."""
    if max_edits < 1 or max_nodes < 1:
        raise ValueError("positive edit and search budgets are required")
    costs = costs or {}
    if any(value < 0 for value in costs.values()):
        raise ValueError("edit costs must be nonnegative")
    groups = witness_directions(witnesses, protected)
    if not groups:
        return SearchResult("passed_declared_probes")
    if any(not group for group in groups):
        return SearchResult("blocked_by_protected_evidence")
    candidates = sorted(
        set().union(*groups), key=lambda item: (costs.get(":".join(item), 1.0), item)
    )
    if len(candidates) > 24:
        return SearchResult("SEARCH_INCOMPLETE", algorithm="local_hitting_set_v1; candidate_cap")
    nodes = 0
    for size in range(1, max_edits + 1):
        for selected in itertools.combinations(candidates, size):
            nodes += 1
            if nodes > max_nodes:
                return SearchResult("SEARCH_INCOMPLETE", nodes=nodes)
            selected_set = set(selected)
            covered = sum(bool(selected_set & group) for group in groups)
            if covered != len(groups):
                continue
            ordered = tuple(sorted(selected, key=lambda item: (item[0], item[1])))
            if evaluator is not None and not evaluator(ordered):
                continue
            cost = sum(costs.get(":".join(item), 1.0) for item in ordered)
            return SearchResult("passed_declared_probes", ordered, cost, covered, nodes)
    return SearchResult("SEARCH_INCOMPLETE", nodes=nodes)


def apply_direction_edits(model, edits: Iterable[Direction]):
    """Return a copy with signed directions cut; the input model is untouched."""
    work = model.copy()
    for reaction_id, direction in edits:
        reaction = work.reactions.get_by_id(reaction_id)
        if direction == "+":
            reaction.upper_bound = min(reaction.upper_bound, 0.0)
        elif direction == "-":
            reaction.lower_bound = max(reaction.lower_bound, 0.0)
        else:
            raise ValueError(f"Unknown reaction direction: {direction}")
    return work
