"""Biomass precursor reachability diagnostics for reconstructed models."""

from __future__ import annotations

import math

from cobra import Reaction

from GemAgents.errors import ToolError
from GemAgents.metabolic.biomass.contracts import classify_biomass_gap
from GemAgents.metabolic.qc import Auditor


def metabolic_biomass_precursor_audit(
    model, biomass: str, *, timeout: int = 30, tolerance: float = 1e-7
) -> dict:
    """Verify joint biomass reachability, then diagnose isolated precursors."""
    if biomass not in model.reactions:
        raise ToolError(f"Biomass precursor audit cannot find reaction {biomass!r}")
    work = model.copy()
    work.solver.configuration.timeout = timeout
    work.tolerance = min(tolerance / 10, 1e-8)
    biomass_reaction = work.reactions.get_by_id(biomass)
    reactants = sorted(
        (
            metabolite.id,
            float(-coefficient),
        )
        for metabolite, coefficient in biomass_reaction.metabolites.items()
        if coefficient < 0
    )
    exchange_ids = {reaction.id for reaction in work.exchanges}
    for boundary in work.boundary:
        if boundary.id in exchange_ids:
            continue
        if len(boundary.metabolites) != 1:
            boundary.bounds = (0, 0)
            continue
        coefficient = next(iter(boundary.metabolites.values()))
        # Side-product drains are often necessary for an otherwise valid
        # precursor pathway. Preserve disposal while forbidding any boundary
        # reaction from supplying an intracellular metabolite.
        if coefficient < 0:
            boundary.bounds = (
                max(0.0, boundary.lower_bound),
                max(0.0, boundary.upper_bound),
            )
        else:
            boundary.bounds = (
                min(0.0, boundary.lower_bound),
                min(0.0, boundary.upper_bound),
            )
    # Biomass ATP hydrolysis can recycle ADP/Pi needed by upstream synthesis.
    # An isolated ATP drain suppresses that recycling and can falsely mark
    # every precursor as absent even while the complete biomass grows. A
    # guarded joint flux is the primary pass criterion; isolated drains are
    # only a diagnostic when joint growth is unavailable.
    with work:
        work.objective = biomass_reaction
        work.objective_direction = "max"
        try:
            joint_solution = work.optimize(raise_error=True)
            joint_growth = float(joint_solution.objective_value)
            if math.isfinite(joint_growth) and joint_growth > tolerance:
                active = [
                    (reaction_id, float(value))
                    for reaction_id, value in joint_solution.fluxes.items()
                    if abs(float(value)) > tolerance
                ]
                active.sort(key=lambda item: (-abs(item[1]), item[0]))
                return {
                    "status": "pass",
                    "method": "joint_biomass_witness",
                    "joint_growth": joint_growth,
                    "tested": len(reactants),
                    "reachable": len(reactants),
                    "failed": [],
                    "incomplete": [],
                    "witness": dict(active[:50]),
                    "rows": [
                        {
                            "metabolite": metabolite_id,
                            "biomass_coefficient": coefficient,
                            "status": "pass",
                            "joint_supported_flux": coefficient * joint_growth,
                        }
                        for metabolite_id, coefficient in reactants
                    ],
                }
        except Exception:
            pass
    biomass_reaction.bounds = (0, 0)
    rows = []
    for index, (metabolite_id, coefficient) in enumerate(reactants):
        with work:
            demand = Reaction(f"__precursor_audit_{index}", lower_bound=0, upper_bound=1000)
            demand.add_metabolites({work.metabolites.get_by_id(metabolite_id): -1.0})
            work.add_reactions([demand])
            work.objective = demand
            work.objective_direction = "max"
            try:
                solution = work.optimize(raise_error=True)
                maximum = float(solution.objective_value)
                if not math.isfinite(maximum):
                    status = "numerical_failure"
                    witness = {}
                else:
                    status = "pass" if maximum > tolerance else "fail"
                    active = [
                        (reaction_id, float(value))
                        for reaction_id, value in solution.fluxes.items()
                        if reaction_id != demand.id and abs(float(value)) > tolerance
                    ]
                    active.sort(key=lambda item: (-abs(item[1]), item[0]))
                    witness = dict(active[:50])
                detail = "declared medium; non-exchange boundaries disposal-only"
            except Exception as error:
                maximum = None
                status = Auditor._solver_error_status(error)
                witness = {}
                detail = f"{type(error).__name__}: {error}"
            metabolite = work.metabolites.get_by_id(metabolite_id)
            chemistry_valid = bool(metabolite.formula) and metabolite.charge is not None
            producers = [
                reaction
                for reaction in metabolite.reactions
                if reaction.id not in {biomass, demand.id} and not reaction.boundary
            ]
            gap_class = None
            if status != "pass":
                gap_class = classify_biomass_gap(
                    solver_status="infeasible" if status == "fail" else status,
                    chemistry_valid=chemistry_valid,
                    template_applicable=bool(producers),
                    transport_supported=True,
                    annotation_supported=any(reaction.genes for reaction in producers),
                )
            rows.append(
                {
                    "metabolite": metabolite_id,
                    "biomass_coefficient": coefficient,
                    "status": status,
                    "maximum": maximum,
                    "witness": witness,
                    "detail": detail,
                    "gap_class": gap_class,
                }
            )
    failed = [row["metabolite"] for row in rows if row["status"] == "fail"]
    incomplete = [row["metabolite"] for row in rows if row["status"] not in {"pass", "fail"}]
    status = "pass" if not failed and not incomplete else "incomplete" if incomplete else "fail"
    return {
        "status": status,
        "method": "isolated_demand_diagnostic",
        "tested": len(rows),
        "reachable": sum(row["status"] == "pass" for row in rows),
        "failed": failed,
        "incomplete": incomplete,
        "rows": rows,
    }
