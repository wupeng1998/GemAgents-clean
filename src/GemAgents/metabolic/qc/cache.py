"""Semantic cache keys and monotonic audit-result reuse.

Cache namespaces are deliberately separate.  A rebuild, annotation or audit
certificate entry can therefore never be mistaken for another kind of result.
Only a cached PASS may be inherited, and only when every current solver bound
is inside the bounds used to establish that PASS.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from threading import RLock
from typing import Any

CACHE_SCHEMA_VERSION = 1
NAMESPACES = frozenset({"rebuild", "annotation", "audit_certificate"})


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical(item) for item in value), key=repr)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("cache payload must contain finite numbers")
        return value
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _canonical(value.as_dict())
    return value


def semantic_digest(namespace: str, payload: Any) -> str:
    if namespace not in NAMESPACES:
        raise ValueError(f"unknown cache namespace: {namespace}")
    encoded = json.dumps(
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "namespace": namespace,
            "payload": _canonical(payload),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def solver_semantics(model) -> dict[str, Any]:
    """Capture solver settings that can change an LP result or its reliability."""
    configuration = model.solver.configuration
    settings = {}
    for name in (
        "presolve",
        "timeout",
        "verbosity",
        "lp_method",
        "qp_method",
    ):
        if hasattr(configuration, name):
            value = getattr(configuration, name)
            settings[name] = value
    tolerances = getattr(configuration, "tolerances", None)
    if tolerances is not None:
        settings["tolerances"] = {
            # optlang backends (notably CPLEX) can reset this solver-side
            # default when a model is copied.  The model-level tolerance is
            # the stable semantic setting used by the auditor and is already
            # captured below, so use it rather than a backend copy artifact.
            "feasibility": float(model.tolerance),
        }
        if any(variable.type != "continuous" for variable in model.solver.variables):
            settings["tolerances"]["integrality"] = getattr(tolerances, "integrality", None)
    interface = model.solver.interface
    return {
        "interface": getattr(interface, "__name__", repr(interface)),
        "configuration": settings,
        "model_tolerance": float(model.tolerance),
    }


def model_structure(model) -> dict[str, Any]:
    """Return order-independent LP structure, excluding mutable variable bounds."""
    reactions = []
    for reaction in model.reactions:
        reactions.append(
            {
                "id": reaction.id,
                "stoichiometry": sorted(
                    (metabolite.id, float(coefficient))
                    for metabolite, coefficient in reaction.metabolites.items()
                ),
            }
        )
    metabolites = sorted(
        (metabolite.id, metabolite.compartment, metabolite.formula, metabolite.charge)
        for metabolite in model.metabolites
    )
    constraints = []
    for constraint in model.solver.constraints:
        constraints.append(
            (
                constraint.name,
                str(constraint.expression),
                constraint.lb,
                constraint.ub,
            )
        )
    variables = sorted((variable.name, variable.type) for variable in model.solver.variables)
    return {
        "reactions": sorted(reactions, key=lambda item: item["id"]),
        "metabolites": metabolites,
        "constraints": sorted(constraints),
        "variables": variables,
        "solver": solver_semantics(model),
    }


def audit_certificate_key(
    model,
    probe,
    *,
    closed: tuple[str, ...] = (),
    tolerance: float,
    timeout: int,
    numerical_strategy: dict[str, Any] | None = None,
) -> str:
    return semantic_digest(
        "audit_certificate",
        {
            "model": model_structure(model),
            "probe": {"name": probe.name, "drains": probe.drains},
            "closed": sorted(set(closed) | {reaction.id for reaction in model.boundary}),
            "tolerance": tolerance,
            "timeout": timeout,
            "numerical_strategy": numerical_strategy or {},
        },
    )


def rebuild_cache_key(**payload: Any) -> str:
    return semantic_digest("rebuild", payload)


def annotation_cache_key(**payload: Any) -> str:
    return semantic_digest("annotation", payload)


@dataclass(frozen=True)
class PassEntry:
    bounds: dict[str, tuple[float, float]]
    detail: str = ""


def bounds_subset(
    current: dict[str, tuple[float, float]], previous: dict[str, tuple[float, float]]
) -> bool:
    if current.keys() != previous.keys():
        return False
    for name, (lower, upper) in current.items():
        old_lower, old_upper = previous[name]
        if lower < old_lower or upper > old_upper:
            return False
    return True


class SemanticCache:
    """Thread-safe in-memory semantic cache for one namespace."""

    def __init__(self, namespace: str) -> None:
        if namespace not in NAMESPACES:
            raise ValueError(f"unknown cache namespace: {namespace}")
        self.namespace = namespace
        self._passes: dict[str, PassEntry] = {}
        self._lock = RLock()

    def put_pass(
        self,
        key: str,
        bounds: dict[str, tuple[float, float]],
        *,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._passes[key] = PassEntry(dict(bounds), detail)

    def inherited_pass(
        self,
        key: str,
        bounds: dict[str, tuple[float, float]],
    ) -> PassEntry | None:
        with self._lock:
            entry = self._passes.get(key)
            if entry is None or not bounds_subset(bounds, entry.bounds):
                return None
            return entry

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._passes)
