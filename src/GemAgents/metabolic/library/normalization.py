"""Deterministic identifiers used while normalizing reaction libraries."""

from __future__ import annotations

from GemAgents.metabolic.ec import (
    metabolic_ec_matches,
    metabolic_normalize_ec,
    metabolic_normalize_ec_values,
)
from GemAgents.metabolic.library.canonicalize import canonical_equation_fingerprint


def metabolic_aliases(value: str, namespace: str = "BiGG") -> list[str]:
    """Read only the requested public ModelSEED alias namespace."""
    values = []
    for part in value.split("|"):
        key, sep, text = part.partition(":")
        if sep and key.strip().casefold() == namespace.casefold():
            values.extend(x.strip() for x in text.split(";") if x.strip())
    return sorted(set(values))


def metabolic_compartment(value: str) -> str:
    """Normalize common compartment names to their one-letter codes."""
    names = {"cytosol": "c", "extracellular": "e", "periplasm": "p"}
    return names.get(value, value.removeprefix("C_"))


def metabolic_equation_key(stoichiometry: dict[str, float]) -> tuple:
    """Return exact, scale- and direction-independent stoichiometry."""
    if not any(stoichiometry.values()):
        return (), 1.0
    fingerprint = canonical_equation_fingerprint(stoichiometry)
    return fingerprint.terms, float(fingerprint.signed_scale)


__all__ = [
    "metabolic_aliases",
    "metabolic_compartment",
    "metabolic_ec_matches",
    "metabolic_equation_key",
    "metabolic_normalize_ec",
    "metabolic_normalize_ec_values",
]
