"""Scope-aware quality suite with explicit incomplete outcomes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Literal

from GemAgents.metabolic.qc.scopes import carrier_coverage
from GemAgents.solver_result import classify_solver_exception

CheckStatus = Literal[
    "pass",
    "fail",
    "not_covered",
    "solver_failure",
    "timeout",
    "numerical_failure",
    "numerical_inconclusive",
]


@dataclass(frozen=True)
class CheckResult:
    name: str
    scope: str
    status: CheckStatus
    required: bool
    detail: str = ""
    metrics: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopeCheck:
    name: str
    scope: str
    runner: Callable[[object, dict[str, object]], CheckResult]
    required: bool = True


class QualitySuite:
    def __init__(self, profile: str, checks: tuple[ScopeCheck, ...]) -> None:
        if not profile or not checks:
            raise ValueError("Quality suite requires a profile and checks")
        if len({check.name for check in checks}) != len(checks):
            raise ValueError("Quality check names must be unique")
        self.profile = profile
        self.checks = checks

    @property
    def scope_hash(self) -> str:
        payload = [
            {"name": check.name, "scope": check.scope, "required": check.required}
            for check in self.checks
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def run(self, model, context: dict[str, object] | None = None) -> tuple[CheckResult, ...]:
        context = dict(context or {})
        results = []
        for check in self.checks:
            try:
                result = check.runner(model, context)
            except Exception as error:
                classified = classify_solver_exception(error)
                status = "timeout" if classified == "timeout" else "solver_failure"
                result = CheckResult(
                    check.name,
                    check.scope,
                    status,
                    check.required,
                    f"{type(error).__name__}: {error}",
                )
            if (result.name, result.scope, result.required) != (
                check.name,
                check.scope,
                check.required,
            ):
                raise ValueError("Check runner returned mismatched identity")
            results.append(result)
        return tuple(results)


def _structure(model, context) -> CheckResult:
    invalid = [r.id for r in model.reactions if r.lower_bound > r.upper_bound]
    return CheckResult(
        "bounds_order",
        "sbml_structure",
        "fail" if invalid else "pass",
        True,
        metrics={"invalid_reactions": invalid},
    )


def _chemistry(model, context) -> CheckResult:
    missing = [
        metabolite.id
        for metabolite in model.metabolites
        if not metabolite.formula or metabolite.charge is None
    ]
    return CheckResult(
        "chemical_fields",
        "chemical_information",
        "fail" if missing else "pass",
        True,
        metrics={"missing": missing},
    )


def _stoichiometry(model, context) -> CheckResult:
    invalid = []
    for reaction in model.reactions:
        if reaction.boundary:
            continue
        try:
            residual = reaction.check_mass_balance()
        except (TypeError, ValueError):
            residual = {"uncheckable": 1.0}
        if residual:
            invalid.append(reaction.id)
    return CheckResult(
        "balanced_internal_reactions",
        "stoichiometric_consistency",
        "fail" if invalid else "pass",
        True,
        metrics={"invalid_reactions": invalid},
    )


def _carriers(model, context) -> CheckResult:
    coverage = carrier_coverage(model)
    return CheckResult(
        "carrier_registry_coverage",
        "energy_carriers",
        "pass" if coverage["covered"] else "not_covered",
        True,
        metrics=coverage,
    )


def _declared(name: str, scope: str):
    def run(model, context) -> CheckResult:
        declared = dict(context.get("scope_results", {})).get(scope)
        if declared is None:
            return CheckResult(name, scope, "not_covered", True, "scope was not executed")
        if isinstance(declared, str):
            status, metrics = declared, {}
        else:
            status = str(declared.get("status", "not_covered"))
            metrics = dict(declared.get("metrics", {}))
        return CheckResult(name, scope, status, True, metrics=metrics)

    return run


def _export_consistency(model, context) -> CheckResult:
    expected = context.get("expected_semantic_hash")
    if expected is None:
        return CheckResult(
            "protected_semantics",
            "export_consistency",
            "not_covered",
            True,
            "expected semantic hash was not supplied",
        )
    from GemAgents.metabolic.qc.certificate import semantic_model_hash

    observed = semantic_model_hash(model)
    return CheckResult(
        "protected_semantics",
        "export_consistency",
        "pass" if observed == expected else "fail",
        True,
        metrics={"expected": expected, "observed": observed},
    )


def default_quality_suite() -> QualitySuite:
    return QualitySuite(
        "gemagents_required_v1",
        (
            ScopeCheck("bounds_order", "sbml_structure", _structure),
            ScopeCheck("chemical_fields", "chemical_information", _chemistry),
            ScopeCheck(
                "balanced_internal_reactions",
                "stoichiometric_consistency",
                _stoichiometry,
            ),
            ScopeCheck(
                "strict_closed_material",
                "strict_closed_material",
                _declared("strict_closed_material", "strict_closed_material"),
            ),
            ScopeCheck("carrier_registry_coverage", "energy_carriers", _carriers),
            ScopeCheck(
                "no_uptake_with_excretion",
                "no_uptake_with_excretion",
                _declared("no_uptake_with_excretion", "no_uptake_with_excretion"),
            ),
            ScopeCheck(
                "biomass_joint_reachability",
                "biomass_joint_reachability",
                _declared("biomass_joint_reachability", "biomass_joint_reachability"),
            ),
            ScopeCheck(
                "medium_phenotype",
                "medium_phenotype",
                _declared("medium_phenotype", "medium_phenotype"),
            ),
            ScopeCheck(
                "gpr_evidence",
                "gpr_evidence",
                _declared("gpr_evidence", "gpr_evidence"),
            ),
            ScopeCheck(
                "reaction_provenance",
                "provenance",
                _declared("reaction_provenance", "provenance"),
            ),
            ScopeCheck(
                "protected_semantics",
                "export_consistency",
                _export_consistency,
            ),
        ),
    )


def results_as_dict(results: tuple[CheckResult, ...]) -> list[dict[str, object]]:
    return [asdict(result) for result in results]
