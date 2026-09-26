"""Explicit biomass assumptions and conservative feasibility diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal

GapClass = Literal[
    "annotation_gap",
    "transport_gap",
    "chemistry_conflict",
    "template_mismatch",
    "numerical_inconclusive",
]


@dataclass(frozen=True)
class BiomassContract:
    source: str
    applicable_taxa: tuple[str, ...]
    reaction_id: str
    coefficients: Mapping[str, float]
    coefficient_unit: str
    gam: float | None = None
    ngam: float | None = None
    trace_precursors: tuple[str, ...] = ()
    control_role: Literal["predicted", "biomass_controlled"] = "predicted"
    assumptions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.reaction_id or not self.coefficient_unit:
            raise ValueError("Biomass source, reaction and coefficient unit are required")
        if not self.applicable_taxa:
            raise ValueError("Biomass applicable_taxa must be declared")
        if not self.coefficients:
            raise ValueError("Biomass coefficients must not be empty")
        if any(not math.isfinite(value) or value == 0 for value in self.coefficients.values()):
            raise ValueError("Biomass coefficients must be finite and nonzero")
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in (self.gam, self.ngam)
        ):
            raise ValueError("Known GAM and NGAM values must be finite and nonnegative")

    @property
    def contract_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def required_precursors(reaction) -> tuple[tuple[str, float], ...]:
    """Return every consumed biomass component without absolute cutoffs."""
    return tuple(
        sorted(
            (metabolite.id, float(-coefficient))
            for metabolite, coefficient in reaction.metabolites.items()
            if coefficient < 0
        )
    )


def classify_biomass_gap(
    *,
    solver_status: str,
    chemistry_valid: bool,
    template_applicable: bool,
    transport_supported: bool,
    annotation_supported: bool,
) -> GapClass:
    if solver_status not in {"optimal", "infeasible"}:
        return "numerical_inconclusive"
    if not chemistry_valid:
        return "chemistry_conflict"
    if not template_applicable:
        return "template_mismatch"
    if not transport_supported:
        return "transport_gap"
    return "annotation_gap" if not annotation_supported else "numerical_inconclusive"
