"""Candidate localization for bounded, evidence-aware gap filling."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateRecord:
    reaction: object
    evidence_class: str
    cost: float = 1.0
    risk: float = 0.0


def localize_candidates(
    candidates: Iterable[CandidateRecord],
    target_metabolites: set[str],
    *,
    allowed_evidence: set[str] | None = None,
    max_risk: float = 0.0,
) -> tuple[CandidateRecord, ...]:
    """Keep only chemistry/evidence-safe candidates touching the local target set."""
    allowed_evidence = allowed_evidence or {
        "sequence_evidence",
        "reference_candidate",
        "strict",
    }
    selected = []
    for candidate in candidates:
        if candidate.evidence_class not in allowed_evidence or candidate.risk > max_risk:
            continue
        metabolites = {metabolite.id for metabolite in candidate.reaction.metabolites}
        if metabolites & target_metabolites:
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: (item.cost + item.risk, item.reaction.id)))
