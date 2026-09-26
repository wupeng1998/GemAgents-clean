"""Deterministic QHEPath-compatible heterologous pathway design.

This module implements the constraint-based part of QHEPath against explicit
SBML/JSON models.  It deliberately does not bundle the upstream repository's
large CSMN assets: callers provide a host model and a cross-species model, so
the model provenance and hashes stay visible in every result.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

QHEPATH_SOURCE = "https://github.com/zxc1852/QHEPath"


@dataclass(frozen=True)
class QHEPathRequest:
    """Validated inputs for one quantitative heterologous pathway run."""

    host_model_path: Path
    network_model_path: Path
    substrate_id: str
    product_id: str
    number_of_optimizations: int = 10
    min_fraction: float = 0.1
    medium: dict[str, float] | None = None

    def __post_init__(self) -> None:
        if not self.substrate_id.strip() or not self.product_id.strip():
            raise ValueError("substrate_id and product_id are required")
        if self.number_of_optimizations < 1:
            raise ValueError("number_of_optimizations must be at least 1")
        if not 0 < self.min_fraction <= 1:
            raise ValueError("min_fraction must be in (0, 1]")
        for path in (self.host_model_path, self.network_model_path):
            if not path.is_file():
                raise FileNotFoundError(path)
        if self.medium is not None:
            if any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
                for key, value in self.medium.items()
            ):
                raise ValueError("medium must map exchange IDs to finite non-negative uptakes")


@dataclass(frozen=True)
class _Solution:
    count: int
    product_flux: float
    product_yield: float
    reaction_ids: tuple[str, ...]
    solver_status: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_model(path: Path) -> Any:
    from cobra.io import load_json_model, read_sbml_model

    suffix = path.suffix.casefold()
    if suffix in {".json", ".gz"} or path.name.casefold().endswith(".json.gz"):
        return load_json_model(str(path))
    return read_sbml_model(str(path))


def _find_exchange(model: Any, metabolite_or_reaction_id: str) -> Any:
    """Resolve a substrate to an exchange reaction without guessing silently."""
    if metabolite_or_reaction_id in model.reactions:
        reaction = model.reactions.get_by_id(metabolite_or_reaction_id)
        if not reaction.boundary:
            raise ValueError(
                "substrate reaction is not a boundary reaction: "
                f"{metabolite_or_reaction_id}"
            )
        return reaction
    base_id = metabolite_or_reaction_id.rsplit("_", 1)[0]
    candidates = [
        f"EX_{metabolite_or_reaction_id}",
        f"EX_{metabolite_or_reaction_id}_e",
        f"EX_{base_id}_e",
        f"EX_{base_id}_c",
    ]
    for reaction_id in candidates:
        if reaction_id in model.reactions and model.reactions.get_by_id(reaction_id).boundary:
            return model.reactions.get_by_id(reaction_id)
    substrate_bases = {metabolite_or_reaction_id, base_id}
    matches = [
        reaction
        for reaction in model.reactions
        if reaction.boundary
        and any(
            metabolite.id in substrate_bases
            or metabolite.id.rsplit("_", 1)[0] in substrate_bases
            for metabolite in reaction.metabolites
        )
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            "substrate has no exchange reaction in model: "
            f"{metabolite_or_reaction_id}"
        )
    raise ValueError(f"substrate is ambiguous in model: {metabolite_or_reaction_id}")


def _product_reaction(model: Any, product_id: str) -> tuple[Any, bool]:
    """Return a product demand/exchange reaction and whether it was added."""
    if product_id in model.reactions:
        return model.reactions.get_by_id(product_id), False
    if product_id not in model.metabolites:
        raise ValueError(f"product metabolite is not present in model: {product_id}")
    reaction_id = f"DM_qhepath_{product_id}"
    if reaction_id in model.reactions:
        return model.reactions.get_by_id(reaction_id), False
    from cobra import Reaction

    reaction = Reaction(reaction_id)
    reaction.lower_bound = 0
    reaction.upper_bound = 1000
    reaction.add_metabolites({model.metabolites.get_by_id(product_id): -1})
    model.add_reactions([reaction])
    return reaction, True


def _configure_model(
    model: Any,
    substrate_id: str,
    product_id: str,
    medium: dict[str, float] | None,
) -> tuple[Any, Any, float]:
    model = model.copy()
    substrate = _find_exchange(model, substrate_id)
    uptake = 1.0
    if medium is not None:
        model.medium = dict(medium)
        if substrate.id not in medium:
            raise ValueError(f"medium does not provide substrate exchange: {substrate.id}")
        uptake = float(medium[substrate.id])
    else:
        # A unit substrate uptake makes the reported product flux a yield.
        _set_uptake_bounds(substrate, 1.0)
    if uptake <= 0:
        raise ValueError("substrate uptake must be positive")
    product, _ = _product_reaction(model, product_id)
    product.lower_bound = max(0.0, product.lower_bound)
    return model, product, uptake


def _set_uptake_bounds(reaction: Any, uptake: float) -> None:
    """Set a positive uptake regardless of exchange stoichiometry convention."""
    coefficient = next(iter(reaction.metabolites.values()), -1.0)
    if coefficient > 0:
        reaction.lower_bound, reaction.upper_bound = 0.0, float(uptake)
    else:
        reaction.lower_bound, reaction.upper_bound = -float(uptake), 0.0


def _copy_reaction(reaction: Any, model: Any) -> Any:
    """Copy a reaction while rebinding metabolites to ``model``."""
    from cobra import Reaction

    copied = Reaction(reaction.id)
    copied.lower_bound = float(reaction.lower_bound)
    copied.upper_bound = float(reaction.upper_bound)
    copied.gene_reaction_rule = reaction.gene_reaction_rule
    metabolites = {}
    for metabolite, coefficient in reaction.metabolites.items():
        if metabolite.id in model.metabolites:
            target = model.metabolites.get_by_id(metabolite.id)
        else:
            from cobra import Metabolite

            target = Metabolite(
                metabolite.id,
                name=metabolite.name,
                compartment=metabolite.compartment,
                formula=metabolite.formula,
                charge=metabolite.charge,
            )
            model.add_metabolites([target])
        metabolites[target] = float(coefficient)
    copied.add_metabolites(metabolites)
    return copied


def _optimize(model: Any) -> Any:
    """Return a status-bearing solution while preserving solver failures."""
    from optlang.exceptions import SolverError

    try:
        return model.optimize()
    except SolverError:
        status = str(getattr(model.solver, "status", "")).casefold()
        if status in {"infeasible", "unbounded"}:
            return SimpleNamespace(status=status, objective_value=None)
        raise


def _ensure_product_metabolite(host: Any, network: Any, product_id: str) -> None:
    """Allow non-native products by importing only the named metabolite metadata."""
    if product_id in host.reactions or product_id in host.metabolites:
        return
    if product_id not in network.metabolites:
        return
    source = network.metabolites.get_by_id(product_id)
    from cobra import Metabolite

    host.add_metabolites(
        [
            Metabolite(
                source.id,
                name=source.name,
                compartment=source.compartment,
                formula=source.formula,
                charge=source.charge,
            )
        ]
    )


def _heterologous_reactions(host: Any, network: Any) -> list[Any]:
    """Return candidate reactions present in the CSMN but absent from the host."""
    host_ids = {reaction.id for reaction in host.reactions}
    candidates = []
    for reaction in network.reactions:
        if reaction.id in host_ids or reaction.boundary:
            continue
        # Biomass and maintenance reactions are host constraints, not pathway genes.
        lowered = reaction.id.casefold()
        if "biomass" in lowered or lowered.startswith(("atpm", "dm_", "sk_")):
            continue
        candidates.append(reaction)
    return sorted(candidates, key=lambda reaction: reaction.id)


def _solve_with_count(
    host: Any,
    product_id: str,
    substrate_id: str,
    candidates: list[Any],
    count: int,
    uptake: float,
    medium: dict[str, float] | None,
    target: float | None = None,
) -> _Solution | None:
    """Maximize product flux with exactly ``count`` selected reactions."""
    model, product, _ = _configure_model(host, substrate_id, product_id, medium)
    # Configure unit uptake after medium setup so the quotient remains a yield.
    substrate = _find_exchange(model, substrate_id)
    _set_uptake_bounds(substrate, uptake)

    added: list[tuple[str, Any]] = []
    for source in candidates:
        if source.id in model.reactions:
            continue
        reaction = _copy_reaction(source, model)
        model.add_reactions([reaction])
        added.append((source.id, reaction))

    if count > len(added):
        return None
    binaries = []
    for index, (_, reaction) in enumerate(added):
        variable = model.problem.Variable(f"qhepath_select_{index}", type="binary")
        model.add_cons_vars(variable)
        binaries.append(variable)
        model.add_cons_vars(
            model.problem.Constraint(
                reaction.flux_expression - reaction.upper_bound * variable,
                ub=0,
                name=f"qhepath_upper_{index}",
            )
        )
        model.add_cons_vars(
            model.problem.Constraint(
                reaction.flux_expression - reaction.lower_bound * variable,
                lb=0,
                name=f"qhepath_lower_{index}",
            )
        )
    model.add_cons_vars(
        model.problem.Constraint(sum(binaries), lb=count, ub=count, name="qhepath_count")
    )
    if target is not None:
        model.add_cons_vars(
            model.problem.Constraint(
                product.flux_expression,
                lb=float(target),
                name="qhepath_target",
            )
        )
    model.objective = product
    model.objective_direction = "max"
    # A solver interruption/failure is propagated.  ``None`` is reserved for
    # a solver that explicitly reports infeasibility.  CPLEX may raise while
    # COBRApy materializes an infeasible solution, so classify only when the
    # backend status is known to be infeasible/unbounded.
    solution = _optimize(model)
    if solution.status == "infeasible":
        return None
    if solution.status != "optimal":
        raise ValueError(f"pathway optimization failed: {solution.status}")
    if solution.objective_value is None:
        raise ValueError("pathway optimization returned no objective value")
    selected = tuple(
        reaction.id
        for (_, reaction), variable in zip(added, binaries, strict=True)
        if float(model.solver.primal_values[variable.name]) > 0.5
    )
    flux = float(solution.objective_value)
    return _Solution(count, flux, flux / uptake, selected, str(solution.status))


def design_qhepath(request: QHEPathRequest) -> dict[str, object]:
    """Compute baseline, maximum-network and ranked heterologous pathways."""
    host_source = _load_model(request.host_model_path)
    network_source = _load_model(request.network_model_path)
    _ensure_product_metabolite(host_source, network_source, request.product_id)
    host, host_product, uptake = _configure_model(
        host_source, request.substrate_id, request.product_id, request.medium
    )
    network, network_product, _ = _configure_model(
        network_source, request.substrate_id, request.product_id, request.medium
    )
    host.objective = host_product
    host.objective_direction = "max"
    baseline_solution = _optimize(host)
    if baseline_solution.status != "optimal" or baseline_solution.objective_value is None:
        raise ValueError(f"baseline optimization failed: {baseline_solution.status}")
    baseline_flux = max(0.0, float(baseline_solution.objective_value))
    network.objective = network_product
    network.objective_direction = "max"
    network_solution = _optimize(network)
    if network_solution.status != "optimal" or network_solution.objective_value is None:
        raise ValueError(f"cross-species model cannot produce product: {network_solution.status}")
    maximum_flux = max(0.0, float(network_solution.objective_value))
    candidates = _heterologous_reactions(host, network)
    threshold = request.min_fraction * maximum_flux

    minimum_syn: _Solution | None = None
    for count in range(len(candidates) + 1):
        minimum_syn = _solve_with_count(
            host,
            request.product_id,
            request.substrate_id,
            candidates,
            count,
            uptake,
            request.medium,
            target=threshold,
        )
        if minimum_syn is not None:
            break
    if minimum_syn is None:
        return {
            "status": "no_feasible_pathway",
            "track": "qhepath",
            "algorithm": "qhepath-compatible",
            "maturity": "experimental",
            "solver_status": "infeasible",
            "baseline_solver_status": str(baseline_solution.status),
            "network_solver_status": str(network_solution.status),
            "pathway_solver_status": "infeasible",
            "baseline_yield": baseline_flux / uptake,
            "maximum_network_yield": maximum_flux / uptake,
            "yield_definition": {
                "numerator": "product_demand_flux",
                "denominator": "configured_substrate_uptake_capacity",
                "substrate_uptake_capacity": uptake,
                "is_consumed_substrate_yield": False,
            },
            "minimum_synthetic_reactions": None,
            "minimum_optimal_reactions": None,
            "pathways": [],
            "model_sha256": {
                "host": _sha256(request.host_model_path),
                "network": _sha256(request.network_model_path),
            },
            "limitations": [
                "No feasible heterologous pathway reached the requested production threshold."
            ],
        }

    minimum_optimal: _Solution | None = None
    for count in range(minimum_syn.count, len(candidates) + 1):
        minimum_optimal = _solve_with_count(
            host,
            request.product_id,
            request.substrate_id,
            candidates,
            count,
            uptake,
            request.medium,
            target=maximum_flux * (1 - 1e-7),
        )
        if minimum_optimal is not None:
            break

    pathways: list[dict[str, object]] = []
    upper = minimum_optimal.count if minimum_optimal is not None else minimum_syn.count
    for count in range(minimum_syn.count, upper + 1):
        solution = _solve_with_count(
            host,
            request.product_id,
            request.substrate_id,
            candidates,
            count,
            uptake,
            request.medium,
        )
        if solution is None:
            continue
        pathways.append(
            {
                "path_id": f"P{len(pathways)}",
                "heterologous_reactions": list(solution.reaction_ids),
                "heterologous_count": solution.count,
                "product_flux": solution.product_flux,
                "product_yield": solution.product_yield,
                "yield_improvement": solution.product_yield - baseline_flux / uptake,
                "fraction_of_network_max": solution.product_flux / maximum_flux
                if maximum_flux
                else 0.0,
                "optimal": bool(minimum_optimal and solution.count == minimum_optimal.count),
                "solver_status": solution.solver_status,
            }
        )
        if len(pathways) >= request.number_of_optimizations:
            break

    return {
        "status": "completed",
        "track": "qhepath",
        "algorithm": "qhepath-compatible",
        "maturity": "experimental",
        "solver_status": "optimal",
        "baseline_solver_status": str(baseline_solution.status),
        "network_solver_status": str(network_solution.status),
        "pathway_solver_status": "optimal",
        "baseline_yield": baseline_flux / uptake,
        "maximum_network_yield": maximum_flux / uptake,
        "yield_definition": {
            "numerator": "product_demand_flux",
            "denominator": "configured_substrate_uptake_capacity",
            "substrate_uptake_capacity": uptake,
            "is_consumed_substrate_yield": False,
        },
        "minimum_synthetic_reactions": minimum_syn.count,
        "minimum_optimal_reactions": minimum_optimal.count if minimum_optimal else None,
        "pathways": pathways,
        "model_sha256": {
            "host": _sha256(request.host_model_path),
            "network": _sha256(request.network_model_path),
        },
        "provenance": {
            "upstream": QHEPATH_SOURCE,
            "host_model": str(request.host_model_path),
            "network_model": str(request.network_model_path),
        },
        "limitations": [
            "Yields are stoichiometric model-condition predictions, not experimental measurements.",
            "The supplied network model is used as the cross-species reaction universe; "
            "no CSMN asset is silently downloaded.",
        ],
    }


def qhepath_design(
    host_model_path: Path | str,
    network_model_path: Path | str,
    substrate_id: str,
    product_id: str,
    *,
    number_of_optimizations: int = 10,
    min_fraction: float = 0.1,
    medium: dict[str, float] | None = None,
) -> dict[str, object]:
    """Convenience API used by the CLI and tool facade."""
    request = QHEPathRequest(
        host_model_path=Path(host_model_path),
        network_model_path=Path(network_model_path),
        substrate_id=substrate_id,
        product_id=product_id,
        number_of_optimizations=number_of_optimizations,
        min_fraction=min_fraction,
        medium=medium,
    )
    return design_qhepath(request)


__all__ = ["QHEPATH_SOURCE", "QHEPathRequest", "design_qhepath", "qhepath_design"]
