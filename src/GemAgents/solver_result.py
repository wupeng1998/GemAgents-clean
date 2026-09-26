"""Solver outcomes: only typed/proven backend infeasibility can lower evidence tiers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

BACKEND_STATUS = {
    "optimal": "optimal",
    "infeasible": "infeasible",
    "nofeasible": "infeasible",
    "unbounded": "unbounded",
    "time_limit": "timeout",
    "numeric": "numerical_failure",
}


@dataclass(frozen=True)
class SolveResult:
    status: str
    backend_status: str | None = None
    exception_type: str | None = None
    error: str | None = None
    feasible_incumbent: bool = False
    objective: float | None = None
    residual: float | None = None
    wall_time: float = 0.0
    time_limit: float | None = None
    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    candidate_pool_expansion_allowed: bool = False
    solution: Any = field(default=None, repr=False, compare=False)

    def event(self):
        return {
            name: getattr(self, name) for name in self.__dataclass_fields__ if name != "solution"
        }


def classify_solver_exception(error: Exception, backend_status: str | None = None) -> str:
    try:
        from cobra.exceptions import Infeasible, OptimizationError, Unbounded
    except ModuleNotFoundError:
        return "timeout" if isinstance(error, TimeoutError) else "internal_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, Infeasible):
        return "infeasible"
    if isinstance(error, Unbounded):
        return "unbounded"
    if isinstance(error, OptimizationError):
        # A genuine solver exception may carry optlang status. Arbitrary program
        # errors must not inherit stale solver status or be classified by wording.
        return BACKEND_STATUS.get(backend_status, "internal_error")
    return "internal_error"


def primal_residual(work, solution) -> float:
    flux = solution.fluxes
    errors = []
    for reaction in work.reactions:
        value = float(flux[reaction.id])
        if not math.isfinite(value):
            return math.inf
        errors.append(max(reaction.lower_bound - value, value - reaction.upper_bound, 0.0))
    for metabolite in work.metabolites:
        errors.append(
            abs(sum(r.metabolites[metabolite] * flux[r.id] for r in metabolite.reactions))
        )
    for variable in work.solver.variables:
        value = variable.primal
        if value is None or not math.isfinite(value):
            return math.inf
        errors.append(
            max(
                (variable.lb - value) if variable.lb is not None else 0.0,
                (value - variable.ub) if variable.ub is not None else 0.0,
                0.0,
            )
        )
        if variable.type in {"binary", "integer"}:
            errors.append(abs(value - round(value)))
    for constraint in work.solver.constraints:
        value = constraint.primal
        if value is None or not math.isfinite(value):
            return math.inf
        errors.append(
            max(
                (constraint.lb - value) if constraint.lb is not None else 0.0,
                (value - constraint.ub) if constraint.ub is not None else 0.0,
                0.0,
            )
        )
    return float(max(errors, default=0.0))


def solve_with_classification(work, *, allow_candidate_expansion: bool = False) -> SolveResult:
    started = perf_counter()
    time_limit = work.solver.configuration.timeout
    try:
        # Returning a non-optimal solution preserves backend status and any incumbent.
        solution = work.optimize(raise_error=False)
    except Exception as error:
        backend = work.solver.status
        status = classify_solver_exception(error, backend)
        return SolveResult(
            status,
            backend_status=backend,
            exception_type=type(error).__name__,
            error=str(error),
            wall_time=perf_counter() - started,
            time_limit=time_limit,
            candidate_pool_expansion_allowed=(status == "infeasible" and allow_candidate_expansion),
        )
    backend = solution.status
    status = BACKEND_STATUS.get(backend, "internal_error")
    objective = solution.objective_value
    objective = float(objective) if objective is not None and math.isfinite(objective) else None
    residual = None
    feasible = False
    if status in {"optimal", "timeout"} or backend == "feasible":
        residual = primal_residual(work, solution)
        feasible = bool(objective is not None and residual <= work.tolerance)
        if status == "optimal" and not feasible:
            status = "numerical_failure"
    # No timeout incumbent is promoted; a separate exploratory verifier would be needed.
    return SolveResult(
        status,
        backend_status=backend,
        objective=objective,
        residual=residual if residual is not None and math.isfinite(residual) else None,
        feasible_incumbent=feasible,
        wall_time=perf_counter() - started,
        time_limit=time_limit,
        solution=solution,
        candidate_pool_expansion_allowed=(status == "infeasible" and allow_candidate_expansion),
    )
