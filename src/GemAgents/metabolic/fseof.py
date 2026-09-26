"""Deterministic FSEOF target analysis for genome-scale metabolic models."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> Any:
    from cobra.io import load_json_model, read_sbml_model

    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.casefold() in {".json", ".gz"} or path.name.casefold().endswith(".json.gz"):
        return load_json_model(str(path))
    return read_sbml_model(str(path))


def _slope(x_values: list[float], y_values: list[float]) -> float:
    if len(x_values) != len(y_values) or len(x_values) < 2:
        raise ValueError("FSEOF requires at least two enforced flux points")
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    if denominator <= 1e-15:
        return 0.0
    return sum(
        (x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values, strict=True)
    ) / denominator


def _validate_medium(medium: dict[str, float] | None) -> None:
    if medium is None:
        return
    if not isinstance(medium, dict):
        raise ValueError("medium must be an object")
    for reaction_id, bound in medium.items():
        if not isinstance(reaction_id, str) or not reaction_id.strip():
            raise ValueError("medium reaction IDs must be non-empty strings")
        if type(bound) not in {int, float} or not math.isfinite(float(bound)):
            raise ValueError("medium bounds must be finite numbers")


def _reaction_record(
    model: Any,
    reaction_id: str,
    slope: float,
    *,
    capacity_slope: float | None = None,
) -> dict[str, object]:
    reaction = model.reactions.get_by_id(reaction_id)
    return {
        "reaction_id": reaction.id,
        "q_slope": float(slope),
        "capacity_slope": None if capacity_slope is None else float(capacity_slope),
        "l_sol": None if capacity_slope is None else float(capacity_slope),
        "reaction": reaction.build_reaction_string(use_metabolite_names=False),
        "reaction_equation": reaction.build_reaction_string(use_metabolite_names=True),
        "compartments": sorted(reaction.compartments),
        "genes": reaction.gene_reaction_rule or None,
    }


def _target_reactions(model: Any) -> list[Any]:
    """Return genetic targets while excluding exchanges and empty GPRs."""
    return [
        reaction
        for reaction in model.reactions
        if not reaction.boundary and reaction.gene_reaction_rule.strip()
    ]


def _knockout_growth(model: Any, biomass_id: str, reaction_id: str) -> float | None:
    work = model.copy()
    work.reactions.get_by_id(reaction_id).bounds = (0.0, 0.0)
    work.objective = work.reactions.get_by_id(biomass_id)
    solution = work.optimize()
    if solution.status != "optimal" or solution.objective_value is None:
        return None
    return float(solution.objective_value)


def run_fseof(
    model_path: Path | str,
    biomass_id: str,
    objective_id: str,
    *,
    steps: int = 30,
    use_fva: bool = False,
    constrain_biomass: bool = False,
    max_flux_cutoff: float = 0.95,
    medium: dict[str, float] | None = None,
) -> dict[str, object]:
    """Find over-expression and down-regulation targets using FSEOF.

    The source model is never changed. ``objective_id`` is the product-forming
    reaction and ``biomass_id`` is restored as the model objective after the
    product ceiling is calculated.  When supplied, ``medium`` is applied only
    to the working copy before the biomass baseline is solved.
    """
    path = Path(model_path)
    if not biomass_id.strip() or not objective_id.strip():
        raise ValueError("biomass_id and objective_id are required")
    if steps < 3:
        raise ValueError("steps must be at least 3")
    if not 0 < max_flux_cutoff <= 1:
        raise ValueError("max_flux_cutoff must be in (0, 1]")
    _validate_medium(medium)
    source = _load(path)
    if biomass_id not in source.reactions:
        raise ValueError(f"biomass reaction is not present in model: {biomass_id}")
    if objective_id not in source.reactions:
        raise ValueError(f"product reaction is not present in model: {objective_id}")

    model = source.copy()
    if medium is not None:
        model.medium = dict(medium)

    # FSEOF's baseline is explicitly the biomass optimum.  The source model's
    # saved objective may be a product, a weighted objective, or a minimization
    # objective and must not silently change the baseline condition.
    model.objective = model.reactions.get_by_id(biomass_id)
    model.objective_direction = "max"
    initial_solution = model.optimize()
    if initial_solution.status != "optimal":
        raise ValueError(f"initial model optimization failed: {initial_solution.status}")
    initial_product_flux = float(initial_solution.fluxes[objective_id])
    optimal_growth = float(initial_solution.fluxes[biomass_id])
    if not math.isfinite(initial_product_flux) or not math.isfinite(optimal_growth):
        raise ValueError("initial FSEOF solution contains a non-finite flux")

    model.objective = model.reactions.get_by_id(objective_id)
    product_solution = model.optimize()
    if product_solution.status != "optimal" or product_solution.objective_value is None:
        raise ValueError(f"product optimization failed: {product_solution.status}")
    max_product_flux = float(product_solution.objective_value)
    if not math.isfinite(max_product_flux):
        raise ValueError("product optimization returned a non-finite objective")
    model.objective = model.reactions.get_by_id(biomass_id)

    if max_product_flux < initial_product_flux - 1e-9:
        raise ValueError("product flux ceiling is below the initial product flux")
    enforced = [
        initial_product_flux + (index / steps) * (max_product_flux - initial_product_flux)
        for index in range(steps - 1)
    ]
    if len(enforced) < 2 or math.isclose(enforced[0], enforced[-1]):
        raise ValueError("FSEOF requires a model with a non-zero product flux range")

    values: dict[str, list[float]] = {reaction.id: [] for reaction in model.reactions}
    capacities: dict[str, list[float]] = {reaction.id: [] for reaction in model.reactions}
    with model:
        if constrain_biomass:
            model.add_cons_vars(
                model.problem.Constraint(
                    model.reactions.get_by_id(biomass_id).flux_expression,
                    lb=max_flux_cutoff * (optimal_growth or 0.0),
                    name="fseof_biomass_floor",
                )
            )
        for index, lower_bound in enumerate(enforced):
            # Each enforced product floor is an independent condition.  Use a
            # nested model context so a previous floor cannot accumulate and
            # flatten all later flux samples to the last constraint.
            with model:
                model.add_cons_vars(
                    model.problem.Constraint(
                        model.reactions.get_by_id(objective_id).flux_expression,
                        lb=lower_bound,
                        name=f"fseof_product_floor_{index}",
                    )
                )
                if use_fva:
                    from cobra.flux_analysis import flux_variability_analysis

                    table = flux_variability_analysis(model)
                    for reaction_id in values:
                        midpoint = float(
                            (
                                table.loc[reaction_id, "maximum"]
                                + table.loc[reaction_id, "minimum"]
                            )
                            / 2
                        )
                        capacity = float(
                            table.loc[reaction_id, "maximum"]
                            - table.loc[reaction_id, "minimum"]
                        )
                        if not math.isfinite(midpoint) or not math.isfinite(capacity):
                            raise ValueError(
                                f"FSEOF FVA returned a non-finite value for {reaction_id}"
                            )
                        values[reaction_id].append(
                            midpoint
                        )
                        capacities[reaction_id].append(capacity)
                else:
                    solution = model.optimize()
                    if solution.status != "optimal":
                        raise ValueError(
                            f"FSEOF optimization failed at step {index}: {solution.status}"
                        )
                    for reaction_id in values:
                        flux = float(solution.fluxes[reaction_id])
                        if not math.isfinite(flux):
                            raise ValueError(
                                f"FSEOF optimization returned a non-finite flux for {reaction_id}"
                            )
                        values[reaction_id].append(flux)

    candidates = _target_reactions(model)
    records = []
    for reaction in candidates:
        reaction_id = reaction.id
        slope = _slope(enforced, values[reaction_id])
        magnitude_slope = _slope(
            enforced, [abs(value) for value in values[reaction_id]]
        )
        capacity_slope = _slope(enforced, capacities[reaction_id]) if use_fva else None
        record = _reaction_record(model, reaction_id, slope, capacity_slope=capacity_slope)
        record["flux_magnitude_slope"] = float(magnitude_slope)
        record["flux_direction"] = (
            "forward"
            if sum(values[reaction_id]) > 1e-9
            else "reverse"
            if sum(values[reaction_id]) < -1e-9
            else "zero"
        )
        if use_fva:
            # Classify by flux magnitude so a reaction encoded in reverse
            # orientation receives the same intervention class as its forward
            # encoding.  Keep the signed slope above for auditability.
            q_class = (
                1
                if magnitude_slope > 1e-9
                else -1
                if magnitude_slope < -1e-9
                else 0
            )
            l_class = (
                1
                if capacity_slope is not None and capacity_slope > 1e-9
                else -1
                if capacity_slope is not None and capacity_slope < -1e-9
                else 0
            )
            record["q_slope_classifier"] = q_class
            record["l_sol_classifier"] = l_class
            record["reaction_class"] = (q_class + 1) * 3 + (l_class + 1)
        records.append(record)

    over = sorted(
        (item for item in records if float(item["flux_magnitude_slope"]) > 1e-9),
        key=lambda item: abs(float(item["flux_magnitude_slope"])),
        reverse=True,
    )
    down = sorted(
        (item for item in records if float(item["flux_magnitude_slope"]) < -1e-9),
        key=lambda item: abs(float(item["flux_magnitude_slope"])),
        reverse=True,
    )
    for item in down:
        item["knockout_growth"] = _knockout_growth(model, biomass_id, str(item["reaction_id"]))
    for item in over:
        item["knockout_growth"] = None

    return {
        "status": "completed",
        "track": "fseof",
        "method": "fseof",
        "model_sha256": _hash(path),
        "biomass_id": biomass_id,
        "objective_id": objective_id,
        "steps": steps,
        "use_fva": use_fva,
        "constrain_biomass": constrain_biomass,
        "max_flux_cutoff": max_flux_cutoff,
        "medium": None if medium is None else dict(medium),
        "solver_status": "optimal",
        "initial_product_flux": initial_product_flux,
        "maximum_product_flux": max_product_flux,
        "optimal_growth": optimal_growth,
        "overexpression_targets": over,
        "downregulation_targets": down,
        "up_targets": over,
        "down_targets": down,
        "limitations": [
            "FSEOF targets are model-condition predictions; gene intervention feasibility "
            "and experimental benefit require independent validation.",
            "Exchange reactions and reactions without a gene-reaction rule are excluded "
            "from genetic target lists.",
        ],
    }


__all__ = ["run_fseof"]
