"""Deterministic model-inspection and perturbation analysis tools.

These operations are deliberately read-only: every operation loads a copy of
the source model, records its SHA-256 identity, and returns structured data for
the language-model orchestrator to explain.  They cover the common inspection,
knockout, and essentiality questions that otherwise tend to become ad-hoc
shell scripts.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from GemAgents.metabolic.analysis import _load, _prepare


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _limit(value: int, *, maximum: int = 5000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer between 1 and {maximum}")
    return value


def _ids(value: list[str], *, label: str, maximum: int = 5000) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    if len(value) > maximum:
        raise ValueError(f"{label} may contain at most {maximum} identifiers")
    normalized = [str(item).strip() for item in value]
    if any(not item for item in normalized):
        raise ValueError(f"{label} must contain non-empty identifiers")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")
    return normalized


def _reaction_record(reaction: Any) -> dict[str, object]:
    return {
        "id": reaction.id,
        "name": reaction.name,
        "lower_bound": float(reaction.lower_bound),
        "upper_bound": float(reaction.upper_bound),
        "subsystem": reaction.subsystem,
        "gene_reaction_rule": reaction.gene_reaction_rule,
        "equation": reaction.reaction,
    }


def inspect_model_component(
    model_path: Path,
    *,
    component: str = "summary",
    identifier: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """Inspect a model or one of its indexed component collections."""
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    component = str(component).casefold().strip()
    aliases = {
        "model": "summary",
        "reaction": "reactions",
        "metabolite": "metabolites",
        "gene": "genes",
        "exchange": "exchanges",
        "compartment": "compartments",
    }
    component = aliases.get(component, component)
    allowed = {"summary", "reactions", "metabolites", "genes", "exchanges", "compartments"}
    if component not in allowed:
        raise ValueError(f"unsupported component: {component}")
    limit = _limit(limit, maximum=5000)
    model = _load(model_path)
    result: dict[str, object] = {
        "status": "completed",
        "operation": "inspect_model_component",
        "component": component,
        "model_sha256": _hash(model_path),
        "model_id": model.id,
    }
    if component == "summary":
        result.update(
            {
                "reactions": len(model.reactions),
                "metabolites": len(model.metabolites),
                "genes": len(model.genes),
                "compartments": dict(model.compartments),
                "exchanges": len(model.exchanges),
                "objective": str(model.objective.expression) if model.objective else None,
            }
        )
        return result
    if component == "reactions":
        if identifier:
            try:
                result["items"] = [_reaction_record(model.reactions.get_by_id(identifier))]
            except KeyError as error:
                raise ValueError(f"reaction is not present in model: {identifier}") from error
        else:
            result["items"] = [_reaction_record(item) for item in model.reactions[:limit]]
    elif component == "metabolites":
        if identifier:
            try:
                items = [model.metabolites.get_by_id(identifier)]
            except KeyError as error:
                raise ValueError(f"metabolite is not present in model: {identifier}") from error
        else:
            items = list(model.metabolites[:limit])
        result["items"] = [
            {
                "id": item.id,
                "name": item.name,
                "formula": item.formula,
                "charge": item.charge,
                "reactions": sorted(reaction.id for reaction in item.reactions),
            }
            for item in items
        ]
    elif component == "genes":
        if identifier:
            try:
                items = [model.genes.get_by_id(identifier)]
            except KeyError as error:
                raise ValueError(f"gene is not present in model: {identifier}") from error
        else:
            items = list(model.genes[:limit])
        result["items"] = [
            {
                "id": item.id,
                "name": item.name,
                "reactions": sorted(reaction.id for reaction in item.reactions),
            }
            for item in items
        ]
    elif component == "exchanges":
        result["items"] = [_reaction_record(item) for item in model.exchanges[:limit]]
    else:
        result["items"] = [
            {"id": str(identifier), "name": str(name)}
            for identifier, name in sorted(model.compartments.items())[:limit]
        ]
    result["returned"] = len(result["items"])
    result["truncated"] = component != "summary" and result["returned"] >= limit
    return result


def _optimize(model: Any) -> dict[str, object]:
    solution = model.optimize()
    objective = solution.objective_value
    return {
        "solver_status": str(solution.status),
        "objective_value": None if objective is None else float(objective),
    }


def simulate_knockouts(
    model_path: Path,
    *,
    kind: str,
    identifiers: list[str],
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
) -> dict[str, object]:
    """Simulate independent gene or reaction knockouts without mutating source bytes."""
    kind = str(kind).casefold().strip()
    if kind not in {"gene", "reaction"}:
        raise ValueError("kind must be gene or reaction")
    names = _ids(identifiers, label="identifiers", maximum=1000)
    model = _prepare(model_path, medium, objective_id=objective_id)
    baseline = _optimize(model)
    results: list[dict[str, object]] = []
    for identifier in names:
        with model:
            try:
                if kind == "gene":
                    model.genes.get_by_id(identifier).knock_out()
                else:
                    reaction = model.reactions.get_by_id(identifier)
                    reaction.lower_bound = 0.0
                    reaction.upper_bound = 0.0
            except KeyError as error:
                raise ValueError(f"{kind} is not present in model: {identifier}") from error
            result = _optimize(model)
        baseline_value = baseline["objective_value"]
        knockout_value = result["objective_value"]
        result.update(
            {
                "identifier": identifier,
                "kind": kind,
                "relative_objective": (
                    None
                    if baseline_value in (None, 0) or knockout_value is None
                    else float(knockout_value) / float(baseline_value)
                ),
            }
        )
        results.append(result)
    return {
        "status": "completed",
        "operation": "simulate_knockouts",
        "model_sha256": _hash(model_path),
        "kind": kind,
        "baseline": baseline,
        "results": results,
        "source_unchanged": True,
        "limitations": [
            "Each perturbation is simulated independently from the same source model.",
            "A knockout growth value is a model-condition prediction, not an experimental result.",
        ],
    }


def scan_essentiality(
    model_path: Path,
    *,
    kind: str = "gene",
    threshold: float = 0.1,
    max_items: int = 1000,
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
) -> dict[str, object]:
    """Scan independent knockouts and classify essentiality at a threshold."""
    kind = str(kind).casefold().strip()
    if kind not in {"gene", "reaction"}:
        raise ValueError("kind must be gene or reaction")
    if type(threshold) not in {int, float} or not 0 <= float(threshold) <= 1:
        raise ValueError("threshold must be between 0 and 1")
    max_items = _limit(max_items, maximum=5000)
    model = _prepare(model_path, medium, objective_id=objective_id)
    collection = model.genes if kind == "gene" else model.reactions
    identifiers = sorted(item.id for item in collection)[:max_items]
    knockout = simulate_knockouts(
        model_path,
        kind=kind,
        identifiers=identifiers,
        medium=medium,
        objective_id=objective_id,
    )
    baseline_value = knockout["baseline"]["objective_value"]
    for item in knockout["results"]:
        value = item["objective_value"]
        item["essential"] = bool(
            baseline_value is not None
            and value is not None
            and float(value) < float(threshold) * float(baseline_value)
        )
    essential = [item["identifier"] for item in knockout["results"] if item["essential"]]
    return {
        "status": "completed",
        "operation": "scan_essentiality",
        "model_sha256": _hash(model_path),
        "kind": kind,
        "threshold": float(threshold),
        "baseline": knockout["baseline"],
        "tested": len(identifiers),
        "total": len(collection),
        "truncated": len(collection) > len(identifiers),
        "essential_identifiers": essential,
        "results": knockout["results"],
        "source_unchanged": True,
        "limitations": [
            "Essentiality is defined by the supplied objective and threshold.",
            "Independent single knockouts do not establish genetic interaction effects.",
        ],
    }


def analyze_shadow_prices(
    model_path: Path,
    *,
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
    limit: int = 5000,
) -> dict[str, object]:
    """Return dual values for metabolites at the optimum."""
    limit = _limit(limit)
    model = _prepare(model_path, medium, objective_id=objective_id)
    solution = model.optimize()
    values = {
        str(key): float(value)
        for key, value in solution.shadow_prices.items()
        if math.isfinite(float(value))
    }
    ordered = sorted(values.items(), key=lambda item: (-abs(item[1]), item[0]))
    return {
        "status": "completed",
        "operation": "analyze_shadow_prices",
        "model_sha256": _hash(model_path),
        "solver_status": str(solution.status),
        "objective_value": (
            None if solution.objective_value is None else float(solution.objective_value)
        ),
        "shadow_prices": dict(ordered[:limit]),
        "returned": min(len(ordered), limit),
        "truncated": len(ordered) > limit,
        "limitations": [
            "Shadow prices are dual values for this model, objective, medium, and solver solution."
        ],
    }
