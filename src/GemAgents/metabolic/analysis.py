"""Read-only, contract-bound model analysis helpers."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class AnalysisContract:
    operation: str
    model_path: str
    model_sha256: str
    objective: str | None
    medium: object | None
    maintenance: float | None
    maintenance_reaction_id: str | None
    solver_tolerance: float | None
    evaluation_track: str
    scenarios: tuple[str, ...] = ()
    scenario_conditions: dict[str, dict[str, float]] | None = None
    network_model_path: str | None = None
    network_model_sha256: str | None = None
    substrate_id: str | None = None
    product_id: str | None = None
    number_of_optimizations: int | None = None
    min_fraction: float | None = None
    biomass_id: str | None = None
    objective_id: str | None = None
    steps: int | None = None
    use_fva: bool = False
    constrain_biomass: bool = False
    max_flux_cutoff: float | None = None
    fraction_of_optimum: float | None = None
    community_models: dict[str, str] | None = None
    community_model_sha256: dict[str, str] | None = None
    strain_design_type: str | None = None
    strain_design_config: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    from cobra.io import load_json_model, read_sbml_model

    if not path.is_file():
        raise FileNotFoundError(path)
    if path.name.casefold().endswith((".json", ".json.gz")):
        return load_json_model(str(path))
    return read_sbml_model(str(path))


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


def _copy_medium(medium: dict[str, float] | None) -> dict[str, float] | None:
    _validate_medium(medium)
    return None if medium is None else {str(key): float(value) for key, value in medium.items()}


def make_contract(
    operation: str,
    model_path: Path,
    *,
    medium: object | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
    evaluation_track: str = "model_condition_prediction",
    scenarios: tuple[str, ...] = (),
    scenario_conditions: dict[str, dict[str, float]] | None = None,
    network_model_path: Path | None = None,
    substrate_id: str | None = None,
    product_id: str | None = None,
    number_of_optimizations: int | None = None,
    min_fraction: float | None = None,
    biomass_id: str | None = None,
    objective_id: str | None = None,
    steps: int | None = None,
    use_fva: bool = False,
    constrain_biomass: bool = False,
    max_flux_cutoff: float | None = None,
    fraction_of_optimum: float | None = None,
    community_models: dict[str, Path] | None = None,
    strain_design_type: str | None = None,
    strain_design_config: dict[str, object] | None = None,
) -> AnalysisContract:
    if medium is not None and not isinstance(medium, dict):
        raise ValueError("medium must be an object")
    medium_copy = _copy_medium(medium)
    if maintenance is not None and (
        type(maintenance) not in {int, float} or not math.isfinite(float(maintenance))
        or float(maintenance) < 0
    ):
        raise ValueError("maintenance must be a finite non-negative number")
    if maintenance is not None and not maintenance_reaction_id:
        raise ValueError("maintenance_reaction_id is required with maintenance")
    if maintenance_reaction_id is not None and not maintenance_reaction_id.strip():
        raise ValueError("maintenance_reaction_id must be a non-empty string")
    if solver_tolerance is not None and (
        type(solver_tolerance) not in {int, float}
        or not math.isfinite(float(solver_tolerance))
        or float(solver_tolerance) <= 0
    ):
        raise ValueError("solver_tolerance must be a positive finite number")
    copied_scenarios = None
    if scenario_conditions is not None:
        if not isinstance(scenario_conditions, dict) or not scenario_conditions:
            raise ValueError("scenario_conditions must be a non-empty object")
        copied_scenarios = {}
        for name, condition in scenario_conditions.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("scenario names and medium maps are required")
            if not isinstance(condition, dict):
                raise ValueError("scenario conditions must be objects")
            copied_scenarios[name] = _copy_medium(condition) or {}
    model = _load(model_path)
    objective = None
    if model.objective is not None:
        objective = str(model.objective.expression)
    return AnalysisContract(
        operation=operation,
        model_path=str(model_path),
        model_sha256=_hash(model_path),
        objective=objective,
        medium=medium_copy,
        maintenance=maintenance,
        maintenance_reaction_id=maintenance_reaction_id,
        solver_tolerance=solver_tolerance,
        evaluation_track=evaluation_track,
        scenarios=tuple(scenarios),
        scenario_conditions=copied_scenarios,
        network_model_path=str(network_model_path) if network_model_path else None,
        network_model_sha256=_hash(network_model_path) if network_model_path else None,
        substrate_id=substrate_id,
        product_id=product_id,
        number_of_optimizations=number_of_optimizations,
        min_fraction=min_fraction,
        biomass_id=biomass_id,
        objective_id=objective_id,
        steps=steps,
        use_fva=use_fva,
        constrain_biomass=constrain_biomass,
        max_flux_cutoff=max_flux_cutoff,
        fraction_of_optimum=fraction_of_optimum,
        community_models=(
            {name: str(Path(path)) for name, path in sorted(community_models.items())}
            if community_models is not None
            else None
        ),
        community_model_sha256=(
            {name: _hash(Path(path)) for name, path in sorted(community_models.items())}
            if community_models is not None
            else None
        ),
        strain_design_type=strain_design_type,
        strain_design_config=(
            dict(strain_design_config) if strain_design_config is not None else None
        ),
    )


def compile_analysis_request(
    prompt: str,
    model_path: Path,
    *,
    network_model_path: Path | None = None,
    substrate_id: str | None = None,
    product_id: str | None = None,
    number_of_optimizations: int = 10,
    min_fraction: float = 0.1,
    medium: dict[str, float] | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
    biomass_id: str | None = None,
    objective_id: str | None = None,
    steps: int = 30,
    use_fva: bool = False,
    constrain_biomass: bool = False,
    max_flux_cutoff: float = 0.95,
    fraction_of_optimum: float = 1.0,
    scenario_conditions: dict[str, dict[str, float]] | None = None,
    community_models: dict[str, Path] | None = None,
    strain_design_type: str | None = None,
    strain_design_config: dict[str, object] | None = None,
) -> AnalysisContract:
    """Compile a narrow set of analysis phrases without inventing a model or medium."""
    text = prompt.casefold()
    smetana = any(
        phrase in text
        for phrase in (
            "smetana",
            "cross-feeding",
            "cross feeding",
            "microbial community",
            "微生物群落",
            "交叉喂养",
            "代谢互作",
        )
    )
    heterologous = any(
        phrase in text
        for phrase in (
            "qhepath",
            "heterologous pathway",
            "heterologous pathways",
            "异源途径",
            "异源路径",
            "异源反应",
        )
    )
    strain_design = any(
        phrase in text
        for phrase in (
            "straindesign",
            "strain design",
            "strain-design",
            "minimal cut set",
            "minimal cut sets",
            "mcs",
            "optknock",
            "robustknock",
            "optcouple",
            "菌株设计",
            "最小割集",
        )
    )
    fseof = any(
        phrase in text
        for phrase in (
            "fseof",
            "overexpression target",
            "over-expression target",
            "downregulation target",
            "down-regulation target",
            "过表达",
            "下调靶点",
            "下调目标",
        )
    )
    if strain_design:
        if not isinstance(strain_design_config, dict):
            raise ValueError("StrainDesign requires strain_design_config")
        config = dict(strain_design_config)
        if strain_design_type is not None and "type" not in config:
            config["type"] = strain_design_type
        return make_contract(
            "strain_design",
            model_path,
            evaluation_track="straindesign",
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
            strain_design_type=strain_design_type,
            strain_design_config=config,
        )
    if smetana:
        if not isinstance(community_models, dict) or len(community_models) < 2:
            raise ValueError("SMETANA requires at least two named community models")
        for name, path in community_models.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("community model names must be non-empty strings")
            if not Path(path).is_file():
                raise FileNotFoundError(path)
        if not 0 < fraction_of_optimum <= 1:
            raise ValueError("fraction_of_optimum must be in (0, 1]")
        return make_contract(
            "pairwise_exchange_screen",
            model_path,
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
            evaluation_track="pairwise_exchange_screen",
            fraction_of_optimum=fraction_of_optimum,
            community_models=community_models,
        )
    if fseof:
        missing = [
            name
            for name, value in (
                ("biomass_id", biomass_id),
                ("objective_id", objective_id),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise ValueError("FSEOF requires " + ", ".join(missing))
        if steps < 3:
            raise ValueError("steps must be at least 3")
        if not 0 < max_flux_cutoff <= 1:
            raise ValueError("max_flux_cutoff must be in (0, 1]")
        return make_contract(
            "fseof_targets",
            model_path,
            evaluation_track="fseof",
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
            biomass_id=biomass_id,
            objective_id=objective_id,
            steps=steps,
            use_fva=use_fva,
            constrain_biomass=constrain_biomass,
            max_flux_cutoff=max_flux_cutoff,
        )
    if heterologous:
        missing = [
            name
            for name, value in (
                ("network_model_path", network_model_path),
                ("substrate_id", substrate_id),
                ("product_id", product_id),
            )
            if value is None or (isinstance(value, str) and not value.strip())
        ]
        if missing:
            raise ValueError(
                "heterologous pathway design requires " + ", ".join(missing)
            )
        if network_model_path is None or not network_model_path.is_file():
            raise FileNotFoundError(network_model_path)
        if number_of_optimizations < 1:
            raise ValueError("number_of_optimizations must be at least 1")
        if not 0 < min_fraction <= 1:
            raise ValueError("min_fraction must be in (0, 1]")
        return make_contract(
            "design_heterologous_pathways",
            model_path,
            evaluation_track="qhepath",
            network_model_path=network_model_path,
            substrate_id=substrate_id,
            product_id=product_id,
            number_of_optimizations=number_of_optimizations,
            min_fraction=min_fraction,
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
    compare = ("compare" in text or "比较" in text) and (
        ("aerobic" in text and "anaerobic" in text)
        or ("有氧" in text and "无氧" in text)
    )
    if compare:
        return make_contract(
            "compare_scenarios",
            model_path,
            scenarios=("aerobic", "anaerobic"),
            scenario_conditions=scenario_conditions,
            objective_id=objective_id,
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
    if "fva" in text or "flux variability" in text or "通量范围" in text:
        if not 0 < fraction_of_optimum <= 1:
            raise ValueError("fraction_of_optimum must be in (0, 1]")
        return make_contract(
            "simulate_fva", model_path, fraction_of_optimum=fraction_of_optimum,
            objective_id=objective_id,
            medium=medium,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
    if "pfba" in text:
        return make_contract(
            "simulate_pfba", model_path,
            medium=medium, objective_id=objective_id, maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
    if "fba" in text or "growth" in text or "生长" in text:
        return make_contract(
            "simulate_fba", model_path,
            medium=medium, objective_id=objective_id, maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
    raise ValueError(
        "analysis request is ambiguous; specify FBA, pFBA, FVA, FSEOF, "
        "heterologous pathway design, StrainDesign, SMETANA community analysis, or "
        "aerobic/anaerobic comparison"
    )


def model_inspect(model_path: Path) -> dict[str, object]:
    model = _load(model_path)
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "model_sha256": _hash(model_path),
        "model_id": model.id,
        "reactions": len(model.reactions),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "objective": str(model.objective.expression) if model.objective else None,
    }


def _prepare(
    model_path: Path,
    medium: dict[str, float] | None,
    *,
    objective_id: str | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
):
    _validate_medium(medium)
    if maintenance is not None and (
        type(maintenance) not in {int, float}
        or not math.isfinite(float(maintenance))
        or float(maintenance) < 0
    ):
        raise ValueError("maintenance must be a finite non-negative number")
    if maintenance is not None and not maintenance_reaction_id:
        raise ValueError("maintenance_reaction_id is required with maintenance")
    if maintenance_reaction_id is not None and not maintenance_reaction_id.strip():
        raise ValueError("maintenance_reaction_id must be a non-empty string")
    if solver_tolerance is not None and (
        type(solver_tolerance) not in {int, float}
        or not math.isfinite(float(solver_tolerance))
        or float(solver_tolerance) <= 0
    ):
        raise ValueError("solver_tolerance must be a positive finite number")
    model = _load(model_path).copy()
    if medium is not None:
        model.medium = dict(medium)
    if objective_id is not None:
        if objective_id not in model.reactions:
            raise ValueError(f"objective reaction is not present in model: {objective_id}")
        model.objective = model.reactions.get_by_id(objective_id)
    if maintenance is not None:
        if maintenance_reaction_id is None or maintenance_reaction_id not in model.reactions:
            raise ValueError(
                "maintenance_reaction_id is required and must be present when maintenance is set"
            )
        reaction = model.reactions.get_by_id(maintenance_reaction_id)
        reaction.lower_bound = max(float(reaction.lower_bound), float(maintenance))
    if solver_tolerance is not None:
        tolerances = getattr(model.solver.configuration, "tolerances", None)
        if tolerances is not None:
            for name in ("feasibility", "optimality"):
                if hasattr(tolerances, name):
                    setattr(tolerances, name, float(solver_tolerance))
    return model


def simulate_fba(
    model_path: Path,
    *,
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
) -> dict[str, object]:
    model = _prepare(
        model_path, medium, objective_id=objective_id, maintenance=maintenance,
        maintenance_reaction_id=maintenance_reaction_id, solver_tolerance=solver_tolerance,
    )
    solution = model.optimize()
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "method": "fba",
        "model_sha256": _hash(model_path),
        "solver_status": solution.status,
        "objective_value": (
            None if solution.objective_value is None else float(solution.objective_value)
        ),
        "fluxes": {key: float(value) for key, value in solution.fluxes.items()},
        "limitations": [
            "An FBA optimum is the maximum objective under the supplied constraints; "
            "it is not proof of a unique flux state used by the cell.",
        ],
    }


def simulate_pfba(
    model_path: Path,
    *,
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
) -> dict[str, object]:
    from cobra.flux_analysis import pfba
    from cobra.util.solver import linear_reaction_coefficients

    model = _prepare(
        model_path,
        medium,
        objective_id=objective_id,
        maintenance=maintenance,
        maintenance_reaction_id=maintenance_reaction_id,
        solver_tolerance=solver_tolerance,
    )
    original_coefficients = linear_reaction_coefficients(model)
    solution = pfba(model)
    fluxes = {key: float(value) for key, value in solution.fluxes.items()}
    original_objective_value = sum(
        float(coefficient) * fluxes[reaction.id]
        for reaction, coefficient in original_coefficients.items()
    )
    total_absolute_flux = sum(abs(value) for value in fluxes.values())
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "method": "pfba",
        "model_sha256": _hash(model_path),
        "solver_status": solution.status,
        # ``solution.objective_value`` is the pFBA second-stage objective
        # (sum of forward and reverse variables), not the model's original
        # biological objective.  Keep the original objective value under the
        # stable public key and expose the two flux-sum readings separately.
        "objective_value": (
            None
            if solution.objective_value is None
            else float(original_objective_value)
        ),
        "pfba_objective_value": (
            None if solution.objective_value is None else float(solution.objective_value)
        ),
        "total_absolute_flux": float(total_absolute_flux),
        "fluxes": fluxes,
        "limitations": [
            "A parsimonious flux solution is not a measurement of the cell's true enzyme cost;"
            " objective_value is the original model objective, while pfba_objective_value"
            " is the second-stage total flux objective.",
        ],
    }


def simulate_fva(
    model_path: Path,
    *,
    fraction_of_optimum: float = 1.0,
    medium: dict[str, float] | None = None,
    objective_id: str | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
) -> dict[str, object]:
    if not 0 < fraction_of_optimum <= 1:
        raise ValueError("fraction_of_optimum must be in (0, 1]")
    from cobra.flux_analysis import flux_variability_analysis

    model = _prepare(
        model_path, medium, objective_id=objective_id, maintenance=maintenance,
        maintenance_reaction_id=maintenance_reaction_id, solver_tolerance=solver_tolerance,
    )
    table = flux_variability_analysis(model, fraction_of_optimum=fraction_of_optimum)
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "method": "fva",
        "model_sha256": _hash(model_path),
        "fraction_of_optimum": fraction_of_optimum,
        "solver_status": "optimality_constrained",
        "flux_ranges": {
            key: {"minimum": float(row.minimum), "maximum": float(row.maximum)}
            for key, row in table.iterrows()
        },
        "limitations": [
            "FVA intervals are not statistical confidence intervals.",
            "The extrema reported for different reactions need not be jointly attainable.",
        ],
    }


def compare_scenarios(
    model_path: Path,
    scenarios: dict[str, dict[str, float]],
    *,
    objective_id: str | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
) -> dict[str, object]:
    if not isinstance(scenarios, dict) or not scenarios:
        raise ValueError("at least one analysis scenario is required")
    source_hash = _hash(model_path)
    results: dict[str, object] = {}
    for name, medium in scenarios.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("scenario names and medium maps are required")
        _validate_medium(medium)
        result = simulate_fba(
            model_path,
            medium=medium,
            objective_id=objective_id,
            maintenance=maintenance,
            maintenance_reaction_id=maintenance_reaction_id,
            solver_tolerance=solver_tolerance,
        )
        results[name] = result
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "method": "scenario_comparison",
        "model_sha256": source_hash,
        "scenarios": results,
        "limitations": [
            "Scenario values are model-condition predictions, not measured growth rates.",
            "The source SBML was not modified.",
        ],
    }


def compare_models(model_paths: dict[str, Path]) -> dict[str, object]:
    if not model_paths:
        raise ValueError("at least one model is required")
    models = {name: model_inspect(path) for name, path in model_paths.items()}
    return {
        "status": "completed",
        "track": "model_condition_prediction",
        "method": "model_comparison",
        "models": models,
        "limitations": ["Model inspection does not establish biological accuracy."],
    }


def render_report(result: dict[str, object]) -> dict[str, object]:
    """Return a structured report whose numbers are copied from tool results."""
    if result.get("status") != "completed":
        return {
            "status": "insufficient_data",
            "track": result.get("track", "model_condition_prediction"),
            "claims": [],
            "limitations": ["The analysis did not complete; no numeric claim is emitted."],
        }
    def finite_number(value: object) -> bool:
        return type(value) in {int, float} and math.isfinite(float(value))

    def solver_ok(value: object) -> bool:
        return value in {"optimal", "optimality_constrained"}

    claims: list[dict[str, object]] = []
    if result.get("track") == "straindesign":
        if not solver_ok(result.get("solver_status")):
            return {
                "status": "insufficient_data",
                "track": result.get("track"),
                "claims": [],
                "limitations": [
                    "No strain-design claim is emitted without an optimal solver status."
                ],
            }
        count = result.get("number_of_solutions")
        if type(count) is not int or count < 0:
            return {
                "status": "insufficient_data",
                "track": result.get("track"),
                "claims": [],
                "limitations": ["The number of strain-design solutions is invalid."],
            }
    requires_solver = any(
        key in result
        for key in ("objective_value", "total_absolute_flux", "flux_ranges", "pathways", "metrics")
    )
    if requires_solver and not solver_ok(result.get("solver_status")):
        return {
            "status": "insufficient_data",
            "track": result.get("track", "model_condition_prediction"),
            "claims": [],
            "limitations": ["No numeric claim is emitted without an optimal solver status."],
        }
    flux_ranges = result.get("flux_ranges")
    if flux_ranges is not None:
        if not isinstance(flux_ranges, dict):
            return {
                "status": "insufficient_data",
                "track": result.get("track", "model_condition_prediction"),
                "claims": [],
                "limitations": ["Flux ranges are not a valid object."],
            }
        for reaction_id, bounds in flux_ranges.items():
            if (
                not isinstance(bounds, dict)
                or not finite_number(bounds.get("minimum"))
                or not finite_number(bounds.get("maximum"))
                or float(bounds["minimum"]) > float(bounds["maximum"])
            ):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track", "model_condition_prediction"),
                    "claims": [],
                    "limitations": [f"Flux range for {reaction_id!r} is invalid."],
                }
    fluxes = result.get("fluxes")
    if fluxes is not None:
        if not isinstance(fluxes, dict) or any(
            not isinstance(reaction_id, str) or not finite_number(value)
            for reaction_id, value in fluxes.items()
        ):
            return {
                "status": "insufficient_data",
                "track": result.get("track", "model_condition_prediction"),
                "claims": [],
                "limitations": ["Flux values are missing or non-finite."],
            }
    if "objective_value" in result:
        if not finite_number(result.get("objective_value")):
            return {
                "status": "insufficient_data",
                "track": result.get("track", "model_condition_prediction"),
                "claims": [],
                "limitations": ["The objective value is missing or non-finite."],
            }
        claims.append(
            {
                "name": "objective_value",
                "value": result.get("objective_value"),
                "source": "tool_result",
                "model_sha256": result.get("model_sha256"),
            }
        )
    if "total_absolute_flux" in result:
        if not finite_number(result.get("total_absolute_flux")):
            return {
                "status": "insufficient_data",
                "track": result.get("track", "model_condition_prediction"),
                "claims": [],
                "limitations": ["The total absolute flux is missing or non-finite."],
            }
        claims.append(
            {
                "name": "total_absolute_flux",
                "value": result.get("total_absolute_flux"),
                "source": "tool_result",
                "model_sha256": result.get("model_sha256"),
            }
        )
    if result.get("method") == "scenario_comparison":
        for name, scenario in dict(result.get("scenarios", {})).items():
            if not isinstance(scenario, dict) or not solver_ok(scenario.get("solver_status")):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track", "model_condition_prediction"),
                    "claims": [],
                    "limitations": [f"Scenario {name!r} has no optimal solver status."],
                }
            if not finite_number(scenario.get("objective_value")):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track", "model_condition_prediction"),
                    "claims": [],
                    "limitations": [f"Scenario {name!r} has no finite objective value."],
                }
            claims.append(
                {
                    "name": f"{name}.objective_value",
                    "value": scenario.get("objective_value"),
                    "source": "tool_result",
                    "model_sha256": result.get("model_sha256"),
                }
            )
    if result.get("track") == "qhepath":
        for name in ("baseline_yield", "maximum_network_yield"):
            if not finite_number(result.get(name)):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track"),
                    "claims": [],
                    "limitations": [f"QHEPath {name} is missing or non-finite."],
                }
            claims.append(
                {
                    "name": name,
                    "value": result.get(name),
                    "source": "tool_result",
                    "model_sha256": result.get("model_sha256"),
                }
            )
        pathways = result.get("pathways", [])
        if not isinstance(pathways, list):
            return {
                "status": "insufficient_data",
                "track": result.get("track"),
                "claims": [],
                "limitations": ["QHEPath pathways are not a valid list."],
            }
        for pathway in pathways:
            if not isinstance(pathway, dict) or not solver_ok(pathway.get("solver_status")):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track"),
                    "claims": [],
                    "limitations": ["A QHEPath pathway lacks an optimal solver status."],
                }
            for key in (
                "product_flux",
                "product_yield",
                "yield_improvement",
                "fraction_of_network_max",
            ):
                if key in pathway and not finite_number(pathway.get(key)):
                    return {
                        "status": "insufficient_data",
                        "track": result.get("track"),
                        "claims": [],
                        "limitations": [f"QHEPath pathway {key} is invalid."],
                    }
    if result.get("track") == "fseof":
        for name in ("initial_product_flux", "maximum_product_flux", "optimal_growth"):
            if not finite_number(result.get(name)):
                return {
                    "status": "insufficient_data",
                    "track": result.get("track"),
                    "claims": [],
                    "limitations": [f"FSEOF {name} is missing or non-finite."],
                }
            claims.append(
                {
                    "name": name,
                    "value": result.get(name),
                    "source": "tool_result",
                    "model_sha256": result.get("model_sha256"),
                }
            )
    if result.get("track") == "pairwise_exchange_screen":
        metrics = result.get("metrics", {})
        if isinstance(metrics, dict):
            for name in ("exchange_pair_candidates", "pairwise_exchange_score"):
                if not finite_number(metrics.get(name)):
                    return {
                        "status": "insufficient_data",
                        "track": result.get("track"),
                        "claims": [],
                        "limitations": [f"Pairwise exchange metric {name} is invalid."],
                    }
                claims.append(
                    {
                        "name": name,
                        "value": metrics.get(name),
                        "source": "tool_result",
                        "community_model_sha256": result.get("community_model_sha256"),
                    }
                )
    if result.get("track") == "straindesign":
        claims.append(
            {
                "name": "number_of_solutions",
                "value": result.get("number_of_solutions"),
                "source": "tool_result",
                "model_sha256": result.get("model_sha256"),
            }
        )
    return {
        "status": "completed",
        "track": result.get("track", "model_condition_prediction"),
        "claims": claims,
        "limitations": list(result.get("limitations", []))
        + [
            "These are model-condition predictions; independent phenotype validation "
            "is not implied."
        ],
    }


def execute_analysis_contract(contract: AnalysisContract) -> dict[str, object]:
    """Execute exactly the conditions captured in ``contract``.

    Contracts are persisted at the tool boundary, so execution verifies source
    bytes before loading a model and never falls back to the caller's mutable
    keyword arguments.
    """
    model_path = Path(contract.model_path)
    if _hash(model_path) != contract.model_sha256:
        raise ValueError("model source changed after the analysis contract was created")
    medium = contract.medium
    if medium is not None and not isinstance(medium, dict):
        raise ValueError("analysis contract medium must be an object")
    if contract.operation == "strain_design":
        if (
            medium is not None
            or contract.maintenance is not None
            or contract.solver_tolerance is not None
        ):
            raise ValueError(
                "StrainDesign does not support medium or solver conditions in this contract"
            )
        from GemAgents.metabolic.straindesign import strain_design

        return strain_design(model_path, dict(contract.strain_design_config or {}))
    if contract.operation == "pairwise_exchange_screen":
        from GemAgents.metabolic.smetana import analyze_community

        if contract.community_models is None:
            raise ValueError("community analysis requires explicit community models")
        expected_hashes = contract.community_model_sha256 or {}
        for name, path in contract.community_models.items():
            current = _hash(Path(path))
            if current != expected_hashes.get(name):
                raise ValueError(
                    "community model source changed after the analysis contract was created: "
                    f"{name}"
                )
        if contract.maintenance is not None or contract.solver_tolerance is not None:
            raise ValueError("community exchange screening does not support solver conditions")
        return analyze_community(
            {name: Path(path) for name, path in contract.community_models.items()},
            medium=medium,
            fraction_of_optimum=float(contract.fraction_of_optimum or 1.0),
        )
    if contract.operation == "design_heterologous_pathways":
        from GemAgents.metabolic.qhepath import qhepath_design

        if (
            contract.network_model_path is None
            or contract.substrate_id is None
            or contract.product_id is None
        ):
            raise ValueError("heterologous pathway design inputs are incomplete")
        if contract.maintenance is not None or contract.solver_tolerance is not None:
            raise ValueError("QHEPath does not support maintenance or solver tolerance conditions")
        network_path = Path(contract.network_model_path)
        if _hash(network_path) != contract.network_model_sha256:
            raise ValueError("network model source changed after the analysis contract was created")
        return qhepath_design(
            model_path, network_path, contract.substrate_id, contract.product_id,
            number_of_optimizations=int(contract.number_of_optimizations or 10),
            min_fraction=float(contract.min_fraction or 0.1),
            medium=medium,
        )
    if contract.operation == "fseof_targets":
        from GemAgents.metabolic.fseof import run_fseof

        if contract.biomass_id is None or contract.objective_id is None:
            raise ValueError("FSEOF requires biomass_id and objective_id")
        if contract.maintenance is not None or contract.solver_tolerance is not None:
            raise ValueError("FSEOF does not support maintenance or solver tolerance conditions")
        return run_fseof(
            model_path, contract.biomass_id, contract.objective_id,
            steps=int(contract.steps or 30), use_fva=contract.use_fva,
            constrain_biomass=contract.constrain_biomass,
            max_flux_cutoff=float(contract.max_flux_cutoff or 0.95),
            medium=medium,
        )
    if contract.operation == "compare_scenarios":
        if contract.scenario_conditions is None:
            raise ValueError("scenario comparison requires explicit medium scenarios")
        return compare_scenarios(
            model_path,
            contract.scenario_conditions,
            objective_id=contract.objective_id,
            maintenance=contract.maintenance,
            maintenance_reaction_id=contract.maintenance_reaction_id,
            solver_tolerance=contract.solver_tolerance,
        )
    common = {
        "medium": medium,
        "objective_id": contract.objective_id,
        "maintenance": contract.maintenance,
        "maintenance_reaction_id": contract.maintenance_reaction_id,
        "solver_tolerance": contract.solver_tolerance,
    }
    if contract.operation == "simulate_fva":
        return simulate_fva(
            model_path, fraction_of_optimum=float(contract.fraction_of_optimum or 1.0), **common
        )
    if contract.operation == "simulate_pfba":
        from inspect import signature

        # The pFBA implementation is allowed to add the same condition
        # keywords as FBA; keeping this call explicit makes an incomplete
        # implementation fail loudly rather than silently dropping them.
        if not all(name in signature(simulate_pfba).parameters for name in common):
            raise ValueError("pFBA implementation cannot apply all contract conditions")
        return simulate_pfba(model_path, **common)
    if contract.operation == "simulate_fba":
        return simulate_fba(model_path, **common)
    raise ValueError(f"unsupported analysis contract operation: {contract.operation}")


def analyze_request(
    prompt: str,
    model_path: Path,
    *,
    scenarios: dict[str, dict[str, float]] | None = None,
    network_model_path: Path | None = None,
    substrate_id: str | None = None,
    product_id: str | None = None,
    number_of_optimizations: int = 10,
    min_fraction: float = 0.1,
    medium: dict[str, float] | None = None,
    maintenance: float | None = None,
    maintenance_reaction_id: str | None = None,
    solver_tolerance: float | None = None,
    biomass_id: str | None = None,
    objective_id: str | None = None,
    steps: int = 30,
    use_fva: bool = False,
    constrain_biomass: bool = False,
    max_flux_cutoff: float = 0.95,
    fraction_of_optimum: float = 1.0,
    community_models: dict[str, Path] | None = None,
    strain_design_type: str | None = None,
    strain_design_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run one deterministic read-only analysis selected from a natural-language request."""
    contract = compile_analysis_request(
        prompt,
        model_path,
        network_model_path=network_model_path,
        substrate_id=substrate_id,
        product_id=product_id,
        number_of_optimizations=number_of_optimizations,
        min_fraction=min_fraction,
        medium=medium,
        maintenance=maintenance,
        maintenance_reaction_id=maintenance_reaction_id,
        solver_tolerance=solver_tolerance,
        biomass_id=biomass_id,
        objective_id=objective_id,
        steps=steps,
        use_fva=use_fva,
        constrain_biomass=constrain_biomass,
        max_flux_cutoff=max_flux_cutoff,
        fraction_of_optimum=fraction_of_optimum,
        scenario_conditions=scenarios,
        community_models=community_models,
        strain_design_type=strain_design_type,
        strain_design_config=strain_design_config,
    )
    if contract.operation == "strain_design" and strain_design_type is not None:
        # ``strain_design_type`` is part of the contract; ensure the config
        # persisted by the contract receives it before execution.
        if contract.strain_design_config is None:
            raise ValueError("StrainDesign requires strain_design_config")
        if "type" not in contract.strain_design_config:
            config = dict(contract.strain_design_config)
            config["type"] = strain_design_type
            contract = replace(contract, strain_design_config=config)
    result = execute_analysis_contract(contract)
    return {"contract": contract.as_dict(), "result": result, "report": render_report(result)}
