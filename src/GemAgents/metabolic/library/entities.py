"""Chemical entity identity independent of display names and reaction IDs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChemicalIdentity:
    """Identity used for conservative metabolite equivalence decisions.

    Compartment and protonation convention are part of identity. Aliases only
    connect records when chemistry is compatible; a shared display name or local
    identifier is never sufficient by itself.
    """

    namespace: str
    accession: str
    compartment: str
    protonation: str
    formula: str | None = None
    charge: float | int | None = None
    aliases: tuple[str, ...] = ()
    source: str | None = None

    def __post_init__(self) -> None:
        required = {
            "namespace": self.namespace,
            "accession": self.accession,
            "compartment": self.compartment,
            "protonation": self.protonation,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"Chemical identity requires {', '.join(missing)}")
        if self.source is None:
            raise ValueError("Chemical identity requires a source or explicit 'unresolved'")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.namespace.casefold(),
            self.accession,
            self.compartment,
            self.protonation,
        )

    def chemically_compatible(self, other: ChemicalIdentity) -> bool:
        if (self.compartment, self.protonation) != (other.compartment, other.protonation):
            return False
        if self.formula and other.formula and self.formula != other.formula:
            return False
        if self.charge is not None and other.charge is not None and self.charge != other.charge:
            return False
        same_record = self.namespace.casefold() == other.namespace.casefold() and (
            self.accession == other.accession
        )
        shared_alias = bool(set(self.aliases) & set(other.aliases))
        cross_referenced = self.accession in other.aliases or other.accession in self.aliases
        return same_record or shared_alias or cross_referenced
