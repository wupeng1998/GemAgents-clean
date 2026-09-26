"""Explicit medium composition and direction-aware nutrient port handling."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True)
class MediumComponent:
    chemical_id: str
    exchange_ids: tuple[str, ...]
    max_uptake: float
    unit: str
    source: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.chemical_id or not self.exchange_ids or not self.unit or not self.source:
            raise ValueError("Medium component identity, ports, unit and source are required")
        if not math.isfinite(self.max_uptake) or self.max_uptake < 0:
            raise ValueError("Medium uptake must be finite and nonnegative")


@dataclass(frozen=True)
class MediumContract:
    profile: str
    source: str
    organism_class: str
    components: tuple[MediumComponent, ...]
    oxygen_status: Literal["aerobic", "anaerobic", "microaerobic", "unknown"]
    temperature_c: float | None = None
    assumptions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile or not self.source or not self.organism_class:
            raise ValueError("Medium profile, source and organism class are required")
        if self.temperature_c is not None and not math.isfinite(self.temperature_c):
            raise ValueError("Known temperature must be finite")
        chemicals = [component.chemical_id for component in self.components]
        if len(set(chemicals)) != len(chemicals):
            raise ValueError("A medium contract must declare one shared budget per chemical")

    @property
    def contract_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def boundary_role(reaction) -> str:
    if reaction.notes.get("audit_only") == "true" or reaction.id.startswith("__probe_"):
        return "audit_only_drain"
    if reaction.id.startswith("DM_"):
        return "demand"
    if reaction.id.startswith("SK_"):
        return "sink"
    if reaction.id.startswith("EX_") or (reaction.boundary and len(reaction.metabolites) == 1):
        return "exchange"
    return "internal"


def _coefficient(reaction) -> float:
    if len(reaction.metabolites) != 1:
        raise ValueError(f"Nutrient port {reaction.id} must contain exactly one metabolite")
    return float(next(iter(reaction.metabolites.values())))


def close_uptake(reaction) -> None:
    """Close only the flux direction that supplies the model."""
    if _coefficient(reaction) < 0:
        reaction.lower_bound = max(0.0, reaction.lower_bound)
    else:
        reaction.upper_bound = min(0.0, reaction.upper_bound)


def _set_uptake(reaction, limit: float) -> None:
    if _coefficient(reaction) < 0:
        reaction.lower_bound = -limit
    else:
        reaction.upper_bound = limit


def close_all_boundaries(model) -> None:
    for reaction in model.boundary:
        reaction.bounds = (0.0, 0.0)


def apply_medium_contract(model, contract: MediumContract) -> dict[str, float]:
    """Apply one budget per chemical, closing duplicate aliases for uptake."""
    for reaction in model.exchanges:
        close_uptake(reaction)
    selected = {}
    for component in contract.components:
        missing = [rid for rid in component.exchange_ids if rid not in model.reactions]
        if missing:
            raise ValueError(f"Unknown nutrient ports for {component.chemical_id}: {missing}")
        ports = [model.reactions.get_by_id(rid) for rid in sorted(component.exchange_ids)]
        canonical, *duplicates = ports
        _set_uptake(canonical, component.max_uptake)
        for reaction in duplicates:
            close_uptake(reaction)
        selected[canonical.id] = component.max_uptake
    model.notes["medium_contract"] = json.dumps(asdict(contract), sort_keys=True)
    model.notes["medium_contract_hash"] = contract.contract_hash
    return selected


def record_applied_medium(model, selected: Mapping[str, float], profile: str) -> dict[str, object]:
    """Record every selected boundary assumption without inventing unknown physiology."""
    components = []
    oxygen_open = False
    for reaction_id, limit in sorted(selected.items()):
        reaction = model.reactions.get_by_id(reaction_id)
        coefficient = _coefficient(reaction)
        metabolite = next(iter(reaction.metabolites))
        formula = str(metabolite.formula or "").replace(" ", "").casefold()
        oxygen_open |= formula == "o2" or metabolite.id.rsplit("_", 1)[0].casefold() in {
            "o2",
            "cpd00007",
        }
        components.append(
            {
                "exchange_id": reaction_id,
                "metabolite_id": metabolite.id,
                "stoichiometric_coefficient": coefficient,
                "uptake_direction": "negative_flux" if coefficient < 0 else "positive_flux",
                "max_uptake": float(limit),
                "unit": "model_flux_unit",
                "source": "GemAgents_preset" if profile != "custom" else "user_declared",
            }
        )
    record = {
        "profile": profile,
        "source": "GemAgents_preset" if profile != "custom" else "user_declared",
        "organism_class": "unknown",
        "oxygen_status": "aerobic" if oxygen_open else "anaerobic_or_oxygen_not_declared",
        "temperature_c": None,
        "components": components,
        "assumptions": {"temperature": "unresolved", "organism_class": "unresolved"},
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    record["contract_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    model.notes["medium_contract"] = json.dumps(record, sort_keys=True)
    model.notes["medium_contract_hash"] = record["contract_hash"]
    return record
