"""Reaction isolation classes and transactional chemistry metadata patches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Literal

ChemistryClass = Literal[
    "strict",
    "unknown_chemistry",
    "imbalanced",
    "empirical_biomass",
    "authorized_rescue",
]


@dataclass(frozen=True)
class ChemistryChange:
    metabolite_id: str
    old_formula: str | None
    old_charge: float | int | None
    new_formula: str | None
    new_charge: float | int | None
    source: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.metabolite_id:
            raise ValueError("metabolite_id is required")
        if not self.source:
            raise ValueError("Chemistry changes require a source or explicit 'unresolved'")


@dataclass
class ChemistryPatch:
    changes: tuple[ChemistryChange, ...]
    affected_reactions: tuple[str, ...]
    before_static: dict[str, dict[str, float | str]]
    after_static: dict[str, dict[str, float | str]]
    before_dynamic: object = None
    after_dynamic: object = None
    status: Literal["applied", "rolled_back"] = "applied"
    rollback_reason: str | None = None
    patch_id: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "changes": [asdict(change) for change in self.changes],
            "affected_reactions": self.affected_reactions,
        }
        self.patch_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _static_result(reaction) -> dict[str, float | str]:
    try:
        residual = reaction.check_mass_balance()
    except (TypeError, ValueError) as error:
        return {"uncheckable": type(error).__name__}
    return {str(key): float(value) for key, value in sorted(residual.items()) if abs(value) > 1e-8}


def classify_reaction(
    reaction,
    *,
    empirical_biomass: bool = False,
    authorized_rescue: bool = False,
) -> ChemistryClass:
    if empirical_biomass:
        return "empirical_biomass"
    if authorized_rescue:
        return "authorized_rescue"
    if any(
        not metabolite.formula or metabolite.charge is None
        for metabolite in reaction.metabolites
    ):
        return "unknown_chemistry"
    return "imbalanced" if _static_result(reaction) else "strict"


def _reaction_map(model) -> dict[str, object]:
    return {reaction.id: reaction for reaction in model.reactions}


def _dependency_closure(model, changes: Iterable[ChemistryChange]) -> tuple[str, ...]:
    changed = {change.metabolite_id for change in changes}
    return tuple(
        sorted(
            reaction.id
            for reaction in model.reactions
            if any(metabolite.id in changed for metabolite in reaction.metabolites)
        )
    )


def apply_chemistry_patch(
    model,
    changes: Iterable[ChemistryChange],
    *,
    dynamic_check: Callable[[object], object] | None = None,
    dynamic_regressed: Callable[[object, object], bool] | None = None,
) -> ChemistryPatch:
    """Apply formula/charge changes atomically and roll back unexplained regressions."""
    changes = tuple(changes)
    if not changes:
        raise ValueError("A chemistry patch requires at least one change")
    if len({change.metabolite_id for change in changes}) != len(changes):
        raise ValueError("A chemistry patch may change each metabolite only once")
    reactions = _reaction_map(model)
    closure = _dependency_closure(model, changes)
    before_all = {rid: _static_result(reaction) for rid, reaction in reactions.items()}
    before_dynamic = dynamic_check(model) if dynamic_check else None
    applied: list[ChemistryChange] = []
    attempted_after_all: dict[str, dict[str, float | str]] = {}
    attempted_after_dynamic = None
    try:
        for change in changes:
            metabolite = model.metabolites.get_by_id(change.metabolite_id)
            if (metabolite.formula, metabolite.charge) != (
                change.old_formula,
                change.old_charge,
            ):
                raise ValueError(f"Stale chemistry patch for {change.metabolite_id}")
            metabolite.formula = change.new_formula
            metabolite.charge = change.new_charge
            applied.append(change)
        after_all = {rid: _static_result(reaction) for rid, reaction in reactions.items()}
        attempted_after_all = after_all
        nonlocal_regressions = [
            rid
            for rid in reactions
            if rid not in closure and not before_all[rid] and after_all[rid]
        ]
        after_dynamic = dynamic_check(model) if dynamic_check else None
        attempted_after_dynamic = after_dynamic
        dynamic_failed = bool(
            dynamic_check
            and dynamic_regressed
            and dynamic_regressed(before_dynamic, after_dynamic)
        )
        if nonlocal_regressions or dynamic_failed:
            reason = (
                f"nonlocal static regression: {nonlocal_regressions}"
                if nonlocal_regressions
                else "dynamic check regressed"
            )
            raise RuntimeError(reason)
    except Exception as error:
        for change in reversed(applied):
            metabolite = model.metabolites.get_by_id(change.metabolite_id)
            metabolite.formula = change.old_formula
            metabolite.charge = change.old_charge
        return ChemistryPatch(
            changes,
            closure,
            {rid: before_all[rid] for rid in closure},
            {
                rid: attempted_after_all.get(rid, _static_result(reactions[rid]))
                for rid in closure
            },
            before_dynamic,
            attempted_after_dynamic,
            status="rolled_back",
            rollback_reason=str(error),
        )
    return ChemistryPatch(
        changes,
        closure,
        {rid: before_all[rid] for rid in closure},
        {rid: after_all[rid] for rid in closure},
        before_dynamic,
        after_dynamic,
    )


def rollback_chemistry_patch(model, patch: ChemistryPatch) -> None:
    for change in patch.changes:
        metabolite = model.metabolites.get_by_id(change.metabolite_id)
        metabolite.formula = change.old_formula
        metabolite.charge = change.old_charge


def replay_chemistry_patch(model, patch: ChemistryPatch) -> ChemistryPatch:
    return apply_chemistry_patch(model, patch.changes)


def validate_isolation_records(records: Iterable[dict[str, object]], *, strict: bool) -> None:
    """Reject missing classes and undeclared non-strict records."""
    allowed = {
        "strict",
        "unknown_chemistry",
        "imbalanced",
        "empirical_biomass",
        "authorized_rescue",
    }
    for record in records:
        chemistry_class = record.get("chemistry_class")
        if chemistry_class not in allowed:
            raise ValueError(f"Reaction {record.get('reaction_id')} lacks a chemistry class")
        if strict and chemistry_class != "strict" and not record.get("isolation_scope"):
            raise ValueError(
                f"Reaction {record.get('reaction_id')} has undeclared non-strict chemistry"
            )
