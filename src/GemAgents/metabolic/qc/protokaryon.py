"""Structural checks for external Protokaryon SBML references.

The Protokaryon directory contains several different library/model layers.
This module only decides whether a selected SBML file is safe to use as a
chemical reference; it never imports reactions, bounds, GPRs, or objectives.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

BOUNDARY_OBJECTIVE_PREFIXES = ("EX_", "DM_", "SK_", "R_EX_", "R_DM_", "R_SK_")


def _objective_ids(model) -> list[str]:
    return sorted(
        {
            str(variable.name).removesuffix("_reverse")
            for variable in model.objective.variables
        }
    )


def _is_boundary_objective(reaction_id: str) -> bool:
    upper = reaction_id.upper()
    return upper.startswith(BOUNDARY_OBJECTIVE_PREFIXES)


def _stoichiometric_scale(source: dict[str, float], candidate: dict[str, float]) -> float | None:
    """Return a common candidate/source coefficient scale, if one exists."""
    if set(source) != set(candidate) or not source:
        return None
    ratios = []
    for metabolite_id, value in source.items():
        other = candidate[metabolite_id]
        if value == 0 or other == 0:
            return None
        ratios.append(other / value)
    scale = ratios[0]
    consistent = all(
        math.isclose(value, scale, rel_tol=1e-9, abs_tol=1e-12) for value in ratios
    )
    return scale if consistent else None


def audit_reference_model(
    model_path: str | Path,
    *,
    comparison_path: str | Path | None = None,
    coefficient_threshold: float = 100.0,
) -> dict[str, Any]:
    """Audit one external SBML model without modifying it.

    Coefficients at or above ``coefficient_threshold`` are flagged as likely
    serialization/assembly errors.  A boundary demand/exchange/sink objective
    is always invalid as a default biomass reference.  When ``comparison_path``
    is supplied, exact-ID equations are compared for uniform scale factors.
    """
    from cobra.io import read_sbml_model

    source = Path(model_path).expanduser().resolve()
    model = read_sbml_model(str(source))
    suspicious: list[dict[str, Any]] = []
    coefficient_bins: Counter[str] = Counter()
    for reaction in model.reactions:
        maximum = max((abs(float(value)) for value in reaction.metabolites.values()), default=0.0)
        if maximum >= coefficient_threshold:
            suspicious.append({"reaction_id": reaction.id, "max_abs_coefficient": maximum})
            coefficient_bins["100_or_more"] += 1
        elif maximum >= 10.0:
            coefficient_bins["10_to_100"] += 1
        else:
            coefficient_bins["below_10"] += 1
    objectives = _objective_ids(model)
    boundary_objectives = [
        reaction_id for reaction_id in objectives if _is_boundary_objective(reaction_id)
    ]
    comparison: dict[str, Any] | None = None
    if comparison_path is not None:
        candidate_path = Path(comparison_path).expanduser().resolve()
        candidate = read_sbml_model(str(candidate_path))
        source_by_id = {reaction.id: reaction for reaction in model.reactions}
        scales: Counter[str] = Counter()
        unmatched = 0
        for reaction in candidate.reactions:
            reference = source_by_id.get(reaction.id)
            if reference is None:
                unmatched += 1
                continue
            source_stoich = {met.id: float(value) for met, value in reference.metabolites.items()}
            candidate_stoich = {met.id: float(value) for met, value in reaction.metabolites.items()}
            scale = _stoichiometric_scale(source_stoich, candidate_stoich)
            if scale is not None and not math.isclose(scale, 1.0, rel_tol=1e-9, abs_tol=1e-12):
                scales[str(round(scale, 12))] += 1
        comparison = {
            "path": str(candidate_path),
            "reactions": len(candidate.reactions),
            "same_id_reaction_count": len(set(source_by_id) & {r.id for r in candidate.reactions}),
            "candidate_only_reactions": unmatched,
            "uniform_scale_factors": dict(sorted(scales.items())),
        }
    valid = not suspicious and not boundary_objectives
    return {
        "schema_version": 1,
        "path": str(source),
        "dimensions": {
            "reactions": len(model.reactions),
            "metabolites": len(model.metabolites),
            "genes": len(model.genes),
        },
        "objective": {
            "reaction_ids": objectives,
            "boundary_objectives": boundary_objectives,
            "status": "invalid_boundary_objective" if boundary_objectives else "review",
        },
        "stoichiometry": {
            "coefficient_threshold": coefficient_threshold,
            "suspicious_reaction_count": len(suspicious),
            "coefficient_bins": dict(sorted(coefficient_bins.items())),
            "suspicious_reactions": suspicious,
        },
        "comparison": comparison,
        "valid_as_chemical_reference": valid,
        "policy": "external_reference_only; no automatic reaction or objective import",
    }


__all__ = ["audit_reference_model"]
