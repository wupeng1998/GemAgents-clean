"""Independent quality certificate bound to model, file, scope and solver policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from GemAgents.metabolic.qc.numerics import NumericalPolicy
from GemAgents.metabolic.qc.suite import CheckResult, QualitySuite


def _semantic_float(value: float) -> float:
    """Canonicalize solver floats before hashing (including signed zero)."""
    number = float(value)
    if number == 0.0:
        return 0.0
    return round(number, 12)


def _semantic_gpr(reaction) -> str:
    """Return a stable representation of a reaction's GPR expression.

    COBRApy may re-parenthesize equivalent ``and``/``or`` expressions while
    round-tripping SBML.  The grouping is semantic, but the serialized text
    is not, so hashing the raw rule makes a valid export look mutated.
    Keep malformed rules verbatim so the certificate still detects them.
    """
    rule = reaction.gene_reaction_rule
    if not rule:
        return ""
    try:
        from cobra.core.gene import GPR

        # SymPy canonicalizes associative/commutative boolean operators, so
        # equivalent parenthesization and operand order have one hash value.
        return str(GPR.from_string(rule).as_symbolic())
    except (SyntaxError, TypeError, ValueError):
        return rule


def semantic_model_payload(model) -> dict[str, object]:
    from cobra.util.solver import linear_reaction_coefficients

    objectives = linear_reaction_coefficients(model)
    return {
        "reactions": [
            {
                "id": reaction.id,
                "bounds": [_semantic_float(value) for value in reaction.bounds],
                "stoichiometry": sorted(
                    (metabolite.id, _semantic_float(coefficient))
                    for metabolite, coefficient in reaction.metabolites.items()
                ),
                "gpr": _semantic_gpr(reaction),
                "objective": _semantic_float(objectives.get(reaction, 0.0)),
            }
            for reaction in sorted(model.reactions, key=lambda item: item.id)
        ],
        "medium": sorted((key, _semantic_float(value)) for key, value in model.medium.items()),
    }


def semantic_model_hash(model) -> str:
    payload = semantic_model_payload(model)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class QualityCertificate:
    profile: str
    model_semantic_hash: str
    file_sha256: str
    scope_hash: str
    solver: str
    numerical_policy: dict[str, object]
    checks: tuple[CheckResult, ...]
    required_checks_complete: bool
    certificate_status: str
    externally_validated: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def verify_export(
    model_path: Path,
    suite: QualitySuite,
    policy: NumericalPolicy,
    *,
    context: dict[str, object] | None = None,
) -> QualityCertificate:
    from cobra.io import read_sbml_model

    model = read_sbml_model(str(model_path))
    results = suite.run(model, context)
    blocking = {
        "fail",
        "not_covered",
        "solver_failure",
        "timeout",
        "numerical_failure",
        "numerical_inconclusive",
    }
    complete = all(not result.required or result.status not in blocking for result in results)
    file_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    external = bool(
        context
        and context.get("external_validation_evidence")
        and any(
            result.scope == "external_phenotype" and result.status == "pass"
            for result in results
        )
    )
    return QualityCertificate(
        suite.profile,
        semantic_model_hash(model),
        file_hash,
        suite.scope_hash,
        policy.solver,
        policy.as_dict(),
        results,
        complete,
        "passed_required_profile" if complete else "blocked_required_profile",
        externally_validated=external,
    )
