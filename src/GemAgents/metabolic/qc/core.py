"""Deterministic CER audit and task-preserving repair."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import TYPE_CHECKING

from GemAgents.metabolic.qc.cache import SemanticCache, audit_certificate_key, bounds_subset

if TYPE_CHECKING:
    from cobra import Model

@dataclass(frozen=True)
class Probe:
    name: str
    # Each entry is one irreversible test reaction; maximize their summed flux.
    drains: tuple[dict[str, float], ...]


@dataclass(frozen=True)
class Task:
    name: str
    objective: str
    minimum: float
    maximum: float | None = None
    # Explicit exchange/boundary bounds, e.g. a complete declared medium.
    bounds: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass
class Result:
    name: str
    status: str
    maximum: float | None = None
    witness: dict[str, float] = field(default_factory=dict)
    residual: float | None = None
    reused: bool = False
    detail: str = ""


def mass_probe(model: Model) -> Probe:
    """Joint nonnegative drains detect net production of any metabolite combination."""
    return Probe("joint_material", tuple({m.id: -1.0} for m in model.metabolites))


class Auditor:
    def __init__(
        self,
        closed: tuple[str, ...] = (),
        *,
        tolerance: float = 1e-7,
        cache: bool = True,
        timeout: int = 30,
        numerical_strategy: dict[str, object] | None = None,
    ) -> None:
        if not math.isfinite(tolerance) or tolerance <= 0 or timeout < 1:
            raise ValueError("positive finite tolerance and positive timeout required")
        self.closed = tuple(sorted(closed))
        self.tolerance = tolerance
        self.cache = cache
        self.timeout = timeout
        self.numerical_strategy = dict(numerical_strategy or {})
        self.lp_calls = 0
        self.cache_hits = 0
        self._cache = SemanticCache("audit_certificate")

    def _key(self, model: Model, probe: Probe) -> str:
        return audit_certificate_key(
            model,
            probe,
            closed=self.closed,
            tolerance=self.tolerance,
            timeout=self.timeout,
            numerical_strategy=self.numerical_strategy,
        )

    @staticmethod
    def _bounds(model: Model) -> dict[str, tuple[float, float]]:
        return {v.name: (v.lb, v.ub) for v in model.solver.variables}

    @staticmethod
    def _subset(current: dict, previous: dict) -> bool:
        return bounds_subset(current, previous)

    def _solve(self, model: Model):
        self.lp_calls += 1
        # Bounds and objectives are changed inside task contexts and after
        # audit-only probe edits.  Refresh optlang's constraint matrix before
        # solving so a reused in-memory model cannot report stale primal
        # residuals even though the exported SBML solves cleanly.
        model.solver.update()
        tolerances = getattr(model.solver.configuration, "tolerances", None)
        if tolerances is not None:
            feasibility = min(self.tolerance / 10.0, 1e-9)
            if tolerances.feasibility > feasibility:
                tolerances.feasibility = feasibility
        solution = model.optimize(raise_error=True)
        if solution.status != "optimal":
            raise RuntimeError(f"non-optimal solve: {solution.status}")
        return solution

    @staticmethod
    def _solver_error_status(error: Exception) -> str:
        """Classify a time-limited audit separately from a failed LP."""
        text = str(error).casefold()
        timeout_markers = ("time limit", "timelimit", "timeout", "time_limit")
        return (
            "incomplete"
            if isinstance(error, TimeoutError) or any(marker in text for marker in timeout_markers)
            else "solver_failure"
        )

    def _residual(self, model: Model, solution) -> float:
        flux = solution.fluxes
        if any(not math.isfinite(value) for value in flux):
            return float("inf")
        errors = [
            abs(sum(r.metabolites[m] * flux[r.id] for r in m.reactions)) for m in model.metabolites
        ]
        errors.extend(
            max(r.lower_bound - flux[r.id], flux[r.id] - r.upper_bound, 0.0)
            for r in model.reactions
        )
        # Also validate any user-supplied solver constraints.
        for c in model.solver.constraints:
            value = c.primal
            if value is None or not math.isfinite(value):
                return float("inf")
            if c.lb is not None:
                errors.append(max(c.lb - value, 0.0))
            if c.ub is not None:
                errors.append(max(value - c.ub, 0.0))
        return max(errors, default=0.0)

    def audit(self, model: Model, probe: Probe) -> Result:
        from cobra import Reaction
        from optlang.symbolics import add as symbolic_add

        if not probe.drains or any(
            not d
            or not any(c < 0 for c in d.values())
            or any(not math.isfinite(c) or c == 0 for c in d.values())
            for d in probe.drains
        ):
            raise ValueError("a probe requires finite, nonzero coefficients and a substrate")
        missing = sorted({m for d in probe.drains for m in d if m not in model.metabolites})
        if missing:
            return Result(probe.name, "not_applicable", detail=f"missing metabolites: {missing}")
        absent = sorted(set(self.closed) - {r.id for r in model.reactions})
        if absent:
            return Result(probe.name, "invalid_configuration", detail=f"missing closure: {absent}")
        if any(r.id.startswith("__cer_probe_") for r in model.reactions):
            raise ValueError("reserved probe reaction prefix already present")

        key = self._key(model, probe) if self.cache else ""
        bounds = self._bounds(model) if self.cache else {}
        inherited = self._cache.inherited_pass(key, bounds) if self.cache else None
        if inherited is not None:
            self.cache_hits += 1
            return Result(
                probe.name,
                "pass",
                reused=True,
                detail="inherited upper bound <= tolerance under bound restriction",
            )

        # A private model copy keeps audit-only relaxations, objectives and solver options isolated.
        work = model.copy()
        relaxed = []
        closed = set(self.closed) | {r.id for r in work.boundary}
        for r in work.reactions:
            if r.id in closed:
                r.bounds = (0, 0)
            elif r.lower_bound > 0 or r.upper_bound < 0:
                relaxed.append(r.id)
                r.bounds = (min(0, r.lower_bound), max(0, r.upper_bound))
        work.solver.configuration.timeout = self.timeout
        work.tolerance = min(self.tolerance / 10, 1e-8)
        test_reactions = []
        for i, drain in enumerate(probe.drains):
            r = Reaction(f"__cer_probe_{i}", lower_bound=0, upper_bound=1000)
            r.add_metabolites({work.metabolites.get_by_id(m): c for m, c in drain.items()})
            test_reactions.append(r)
        work.add_reactions(test_reactions)
        expression = symbolic_add(
            tuple(v for r in test_reactions for v in (r.forward_variable, -r.reverse_variable))
        )
        work.objective = work.problem.Objective(expression, direction="max")
        try:
            solution = self._solve(work)
            maximum = float(solution.objective_value)
            residual = self._residual(work, solution)
            if not math.isfinite(maximum) or residual > self.tolerance:
                return Result(probe.name, "numerical_failure", residual=residual)
            detail = f"strict boundary closure; audit-only forced-flux relaxation: {relaxed}"
            if maximum <= self.tolerance:
                if self.cache:
                    self._cache.put_pass(key, bounds, detail=detail)
                return Result(probe.name, "pass", maximum, residual=residual, detail=detail)

            # A parsimonious witness is usually small, but is not a minimum-cardinality cycle.
            minimum = min(1.0, maximum)
            work.add_cons_vars(work.problem.Constraint(expression, lb=minimum, name="cer_leak"))
            work.objective = work.problem.Objective(
                symbolic_add(
                    tuple(
                        v for r in work.reactions for v in (r.forward_variable, r.reverse_variable)
                    )
                ),
                direction="min",
            )
            solution = self._solve(work)
            residual = self._residual(work, solution)
            if residual > self.tolerance:
                return Result(probe.name, "numerical_failure", residual=residual)
            witness = {
                r.id: float(solution.fluxes[r.id])
                for r in model.reactions
                if r.id not in closed and abs(solution.fluxes[r.id]) > self.tolerance
            }
            return Result(probe.name, "fail", maximum, witness, residual, detail=detail)
        except Exception as error:
            # Non-optimal, infeasible and unbounded solves fail; a timeout is
            # explicitly incomplete so a short audit can never be mistaken for
            # a passed probe.
            return Result(
                probe.name,
                self._solver_error_status(error),
                detail=f"{type(error).__name__}: {error}",
            )

    def tasks(self, model: Model, tasks: tuple[Task, ...]) -> list[Result]:
        results = []
        boundary_ids = {r.id for r in model.boundary}
        for task in tasks:
            if not math.isfinite(task.minimum) or (
                task.maximum is not None
                and (not math.isfinite(task.maximum) or task.maximum < task.minimum)
            ):
                raise ValueError("invalid task thresholds")
            if set(task.bounds) - boundary_ids:
                raise ValueError("task overrides may only change boundary reactions")
            with model:
                try:
                    for rid, bounds in task.bounds.items():
                        model.reactions.get_by_id(rid).bounds = bounds
                    model.objective = model.reactions.get_by_id(task.objective)
                    model.objective_direction = "max"
                    solution = self._solve(model)
                    value = float(solution.objective_value)
                    residual = self._residual(model, solution)
                    valid = math.isfinite(value) and residual <= self.tolerance
                    valid &= value >= task.minimum - self.tolerance
                    valid &= task.maximum is None or value <= task.maximum + self.tolerance
                    results.append(
                        Result(task.name, "pass" if valid else "fail", value, residual=residual)
                    )
                except Exception as error:
                    results.append(
                        Result(
                            task.name,
                            self._solver_error_status(error),
                            detail=f"{type(error).__name__}: {error}",
                        )
                    )
        return results


def repair(
    model: Model,
    probes: tuple[Probe, ...],
    tasks: tuple[Task, ...],
    auditor: Auditor,
    *,
    costs: dict[str, float] | None = None,
    protected: tuple[str, ...] = (),
    max_edits: int = 20,
) -> tuple[Model, dict]:
    """Greedy signed restrictions with task guards; return a new model and full log."""
    if not probes or not tasks or max_edits < 0:
        raise ValueError("nonempty probes/tasks and nonnegative edit budget required")
    if len({p.name for p in probes}) != len(probes):
        raise ValueError("probe names must be unique")
    if any(not math.isfinite(c) or c < 0 for c in (costs or {}).values()):
        raise ValueError("edit costs must be finite and nonnegative")
    start = perf_counter()
    work = model.copy()
    work.solver.configuration.timeout = auditor.timeout
    work.tolerance = min(auditor.tolerance / 10, 1e-8)
    blocked = set(protected) | set(auditor.closed) | {r.id for r in work.boundary}
    blocked |= {t.objective for t in tasks}
    attempts, edits, rounds = [], [], []
    initial_tasks = auditor.tasks(work, tasks)
    status = "initial_task_failure"
    if all(r.status == "pass" for r in initial_tasks):
        for step in range(max_edits + 1):
            results = [auditor.audit(work, p) for p in probes]
            rounds.append([asdict(r) for r in results])
            if any(r.status not in {"pass", "fail"} for r in results):
                status = "incomplete_audit"
                break
            failures = [r for r in results if r.status == "fail"]
            if not failures:
                status = "passed_declared_probes"
                break
            if step == max_edits:
                status = "budget_exhausted"
                break
            # Collect directions from all current witnesses; cheap edits get tried first.
            directions = {
                (rid, "+" if flux > 0 else "-")
                for failure in failures
                for rid, flux in failure.witness.items()
                if rid not in blocked
            }
            ordered = sorted(directions, key=lambda x: ((costs or {}).get(":".join(x), 1), x))
            accepted = False
            for rid, sign in ordered:
                r = work.reactions.get_by_id(rid)
                old = r.bounds
                new = (old[0], min(old[1], 0)) if sign == "+" else (max(old[0], 0), old[1])
                if new[0] > new[1] or old == new:
                    continue
                r.bounds = new
                checks = auditor.tasks(work, tasks)
                valid = all(c.status == "pass" for c in checks)
                attempt = {
                    "reaction": rid,
                    "direction": sign,
                    "before": old,
                    "after": new,
                    "accepted": valid,
                    "tasks": [asdict(c) for c in checks],
                }
                attempts.append(attempt)
                if valid:
                    edits.append(attempt)
                    accepted = True
                    break
                r.bounds = old
            if not accepted:
                status = "no_task_preserving_single_cut"
                break
    # Always independently rerun all final probes without cache: a reviewable final audit.
    verifier = Auditor(
        auditor.closed, tolerance=auditor.tolerance, cache=False, timeout=auditor.timeout
    )
    final = [verifier.audit(work, p) for p in probes]
    final_tasks = verifier.tasks(work, tasks)
    if status == "passed_declared_probes" and (
        any(r.status != "pass" for r in final + final_tasks)
    ):
        status = "final_verification_failure"
    return work, {
        "status": status,
        "scope": "declared strict-closure probes only; no biological/thermodynamic guarantee",
        "initial_tasks": [asdict(r) for r in initial_tasks],
        "rounds": rounds,
        "attempts": attempts,
        "edits": edits,
        "final_probes": [asdict(replace(r, reused=False)) for r in final],
        "final_tasks": [asdict(r) for r in final_tasks],
        "search_lp_calls": auditor.lp_calls,
        "final_verification_lp_calls": verifier.lp_calls,
        "cache_hits": auditor.cache_hits,
        "wall_seconds": perf_counter() - start,
    }
