"""Read-only StrainDesign adapter for model analysis.

The upstream package owns the strain-design algorithms.  This module keeps the
GemAgents boundary deterministic: it validates a JSON-compatible setup, loads a
copy of the supplied model, invokes StrainDesign, and returns JSON data with
the source hash and limitations attached.
"""

from __future__ import annotations

import ctypes
import hashlib
import math
import numbers
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STRAINDESIGN_SOURCE = "https://straindesign.readthedocs.io/en/latest/"
_MODULE_TYPES = {"mcs", "optknock", "robustknock", "optcouple"}
_SOLVERS = {"cplex", "gurobi", "glpk", "scip"}
_SOLUTION_APPROACHES = {"any", "best", "populate"}


@dataclass(frozen=True)
class StrainDesignRequest:
    """Validated, serializable inputs for one strain-design computation."""

    model_path: Path
    config: dict[str, object]

    def __post_init__(self) -> None:
        if not self.model_path.is_file():
            raise FileNotFoundError(self.model_path)
        if not isinstance(self.config, dict):
            raise ValueError("strain_design_config must be an object")
        _validate_config(self.config)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_model(path: Path) -> Any:
    from cobra.io import load_json_model, read_sbml_model

    if not path.is_file():
        raise FileNotFoundError(path)
    if path.name.casefold().endswith((".json", ".json.gz")):
        return load_json_model(str(path))
    return read_sbml_model(str(path))


def _as_constraints(value: object, field: str, *, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise ValueError(f"strain_design_config.{field} is required")
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise ValueError(f"strain_design_config.{field} must be a non-empty string or list")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"strain_design_config.{field} must contain non-empty strings")
    return [item.strip() for item in values]


def _validate_costs(config: dict[str, object], field: str) -> None:
    value = config.get(field)
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"strain_design_config.{field} must be an object")
    for key, cost in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"strain_design_config.{field} keys must be non-empty strings")
        if type(cost) not in {int, float} or not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError(
                f"strain_design_config.{field} values must be finite non-negative numbers"
            )


def _validate_config(config: dict[str, object]) -> None:
    allowed = {
        "type",
        "constraints",
        "suppress_constraints",
        "protect_constraints",
        "inner_objective",
        "outer_objective",
        "prod_id",
        "inner_opt_sense",
        "outer_opt_sense",
        "min_gcp",
        "max_solutions",
        "max_cost",
        "time_limit",
        "solver",
        "solution_approach",
        "gene_kos",
        "compress",
        "ko_cost",
        "ki_cost",
        "gko_cost",
        "gki_cost",
        "reg_cost",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            "strain_design_config unknown fields: " + ", ".join(sorted(unknown))
        )
    module_type = str(config.get("type", "mcs")).casefold()
    if module_type not in _MODULE_TYPES:
        raise ValueError(f"strain_design_config.type must be one of {sorted(_MODULE_TYPES)}")
    if module_type == "mcs":
        suppress = config.get("suppress_constraints", config.get("constraints"))
        _as_constraints(suppress, "suppress_constraints", required=True)
        _as_constraints(config.get("protect_constraints"), "protect_constraints")
    else:
        _as_constraints(config.get("constraints"), "constraints")
        for field in ("inner_objective", "outer_objective", "prod_id"):
            if field in config and config[field] is not None and (
                not isinstance(config[field], str) or not config[field].strip()
            ):
                raise ValueError(f"strain_design_config.{field} must be a non-empty string")
        required = {
            "optknock": ("inner_objective", "outer_objective"),
            "robustknock": ("inner_objective", "outer_objective"),
            "optcouple": ("inner_objective", "prod_id"),
        }[module_type]
        missing = [field for field in required if not config.get(field)]
        if missing:
            raise ValueError("StrainDesign requires " + ", ".join(missing))
    for field in ("inner_opt_sense", "outer_opt_sense"):
        if config.get(field) is not None and config[field] not in {"maximize", "minimize"}:
            raise ValueError(f"strain_design_config.{field} must be maximize or minimize")
    for field in ("max_solutions", "time_limit"):
        value = config.get(field)
        if value is not None and (type(value) not in {int, float} or value <= 0):
            raise ValueError(f"strain_design_config.{field} must be positive")
    max_cost = config.get("max_cost")
    if max_cost is not None and (
        type(max_cost) not in {int, float} or not math.isfinite(float(max_cost)) or max_cost < 0
    ):
        raise ValueError("strain_design_config.max_cost must be finite and non-negative")
    for field in ("solver", "solution_approach"):
        value = config.get(field)
        allowed_values = _SOLVERS if field == "solver" else _SOLUTION_APPROACHES
        if value is not None and value not in allowed_values:
            raise ValueError(
                f"strain_design_config.{field} must be one of {sorted(allowed_values)}"
            )
    for field in ("gene_kos", "compress"):
        if field in config and type(config[field]) is not bool:
            raise ValueError(f"strain_design_config.{field} must be a boolean")
    for field in ("ko_cost", "ki_cost", "gko_cost", "gki_cost"):
        _validate_costs(config, field)
    if config.get("reg_cost") is not None and not isinstance(config["reg_cost"], dict):
        raise ValueError("strain_design_config.reg_cost must be an object")


def _module_kwargs(config: dict[str, object]) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    constraints = _as_constraints(config.get("constraints"), "constraints")
    if constraints:
        kwargs["constraints"] = constraints
    for field in (
        "inner_objective",
        "outer_objective",
        "prod_id",
        "inner_opt_sense",
        "outer_opt_sense",
        "min_gcp",
    ):
        if config.get(field) is not None:
            kwargs[field] = config[field]
    return kwargs


def _import_straindesign() -> Any:
    """Load the conda C++ runtime before matplotlib/CPLEX extensions.

    Some conda builds of StrainDesign's plotting dependency need a newer
    ``libstdc++`` than the host system.  Preloading the environment copy is
    harmless when it is already selected and keeps the optional adapter usable
    without requiring callers to export ``LD_LIBRARY_PATH`` manually.
    """
    runtime = Path(sys.prefix) / "lib" / "libstdc++.so.6"
    if runtime.is_file():
        ctypes.CDLL(str(runtime), mode=ctypes.RTLD_GLOBAL)
    import straindesign as sd

    return sd


def _make_modules(model: Any, config: dict[str, object]) -> list[Any]:
    import straindesign as sd

    module_type = str(config.get("type", "mcs")).casefold()
    if module_type != "mcs":
        return [sd.SDModule(model, module_type, **_module_kwargs(config))]
    modules = [
        sd.SDModule(
            model,
            sd.SUPPRESS,
            constraints=_as_constraints(
                config.get("suppress_constraints", config.get("constraints")),
                "suppress_constraints",
                required=True,
            ),
        )
    ]
    protect = _as_constraints(config.get("protect_constraints"), "protect_constraints")
    if protect:
        modules.append(sd.SDModule(model, sd.PROTECT, constraints=protect))
    return modules


def design_strain(request: StrainDesignRequest) -> dict[str, object]:
    """Run StrainDesign on a model copy and return JSON-compatible results."""
    config = dict(request.config)
    try:
        sd = _import_straindesign()
    except ImportError as error:
        raise RuntimeError(
            "StrainDesign is unavailable; install the optional 'metabolic' dependencies"
        ) from error
    model = _load_model(request.model_path).copy()
    modules = _make_modules(model, config)
    kwargs: dict[str, object] = {
        "sd_modules": modules,
        "max_solutions": int(config.get("max_solutions", 10)),
        "solution_approach": str(config.get("solution_approach", "best")),
        "gene_kos": bool(config.get("gene_kos", False)),
        "compress": bool(config.get("compress", True)),
    }
    for field in (
        "max_cost",
        "time_limit",
        "solver",
        "ko_cost",
        "ki_cost",
        "gko_cost",
        "gki_cost",
        "reg_cost",
    ):
        if config.get(field) is not None:
            kwargs[field] = config[field]
    solutions = sd.compute_strain_designs(model, **kwargs)
    designs = []
    for design in list(solutions.get_reaction_sd())[: int(config.get("max_solutions", 10))]:
        designs.append(
            {
                str(key): (
                    int(value)
                    if isinstance(value, numbers.Real) and float(value).is_integer()
                    else float(value)
                    if isinstance(value, numbers.Real)
                    else value
                )
                for key, value in design.items()
            }
        )
    return {
        "status": "completed",
        "track": "straindesign",
        "algorithm": "straindesign",
        "design_type": str(config.get("type", "mcs")).casefold(),
        "solver_status": str(solutions.status),
        "number_of_solutions": len(designs),
        "solutions": designs,
        "model_sha256": _sha256(request.model_path),
        "configuration": config,
        "provenance": {"upstream": STRAINDESIGN_SOURCE, "model": str(request.model_path)},
        "limitations": [
            "Strain designs are stoichiometric model-condition predictions, not "
            "experimental measurements.",
            "The source model is loaded and copied; no model file is modified.",
        ],
    }


def strain_design(
    model_path: Path | str, config: dict[str, object] | None = None
) -> dict[str, object]:
    """Convenience API used by the analysis dispatcher."""
    request = StrainDesignRequest(Path(model_path), dict(config or {}))
    return design_strain(request)


__all__ = ["STRAINDESIGN_SOURCE", "StrainDesignRequest", "design_strain", "strain_design"]
