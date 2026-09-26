"""Lossless equation equivalence with explicit direction and flux scaling."""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from math import isfinite


@dataclass(frozen=True)
class EquationFingerprint:
    """Canonical equation plus the mapping back to the source flux unit."""

    terms: tuple[tuple[str, str], ...]
    direction: int
    scale: str

    @property
    def signed_scale(self) -> Fraction:
        return Fraction(self.scale) * self.direction

    def same_chemistry(self, other: EquationFingerprint) -> bool:
        return self.terms == other.terms

    def source_to(self, representative: EquationFingerprint) -> Fraction:
        """Return factor mapping source flux to representative flux."""
        if not self.same_chemistry(representative):
            raise ValueError("Equations describe different chemistry")
        return self.signed_scale / representative.signed_scale


def _key(value: Hashable) -> str:
    if hasattr(value, "key"):
        parts = value.key  # type: ignore[attr-defined]
        return "\x1f".join(str(part) for part in parts)
    return str(value)


def canonical_equation_fingerprint(
    stoichiometry: Mapping[Hashable, int | float | str | Fraction],
) -> EquationFingerprint:
    """Normalize exact ratios while retaining orientation and absolute scale."""
    coefficients: dict[str, Fraction] = {}
    for entity, raw in stoichiometry.items():
        if isinstance(raw, float) and not isfinite(raw):
            raise ValueError("Stoichiometric coefficients must be finite")
        value = Fraction(str(raw))
        if value:
            key = _key(entity)
            coefficients[key] = coefficients.get(key, Fraction()) + value
    items = sorted((key, value) for key, value in coefficients.items() if value)
    if not items:
        raise ValueError("An equation requires at least one nonzero coefficient")
    pivot = items[0][1]
    direction = 1 if pivot < 0 else -1
    scale = abs(pivot)
    canonical = tuple((key, str(value / scale * direction)) for key, value in items)
    return EquationFingerprint(canonical, direction, str(scale))
