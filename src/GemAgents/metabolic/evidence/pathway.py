"""Reviewable pathway candidates and isolated model patch dry-runs.

The sandbox deliberately stops before an accepted model pointer.  Candidates
must carry structured provenance and independent chemical/enzyme statuses;
analysis after approval always runs on a copy of the source model.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from .requests import EvidenceResult

_CHEMICAL_STATUSES = {"verified", "unknown", "contradictory"}
_ENZYME_STATUSES = {"supported", "unknown", "unsupported"}
_MODEL_STATUSES = {"feasible", "unknown", "infeasible"}
_MISSING = object()


def model_fingerprint(model: Any) -> str:
    """Hash model structure without serializing or mutating the model."""
    reactions = []
    for reaction in sorted(model.reactions, key=lambda item: item.id):
        reactions.append(
            {
                "id": reaction.id,
                "bounds": [float(reaction.lower_bound), float(reaction.upper_bound)],
                "gpr": reaction.gene_reaction_rule,
                "metabolites": sorted(
                    (metabolite.id, float(coefficient))
                    for metabolite, coefficient in reaction.metabolites.items()
                ),
            }
        )
    payload = json.dumps(reactions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class PathwayCandidate:
    candidate_id: str
    reaction_id: str
    stoichiometry: dict[str, float]
    source_id: str
    source_version: str
    source_record_id: str
    chemical_status: str
    enzyme_status: str
    gpr: str = ""
    required_conditions: tuple[str, ...] = ()
    experimental_status: str = "unknown"
    model_feasibility: str = "unknown"

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.reaction_id.strip():
            raise ValueError("pathway candidates require IDs")
        if not self.stoichiometry:
            raise ValueError("pathway candidates require stoichiometry")
        if any(
            not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in self.stoichiometry.values()
        ):
            raise ValueError("candidate stoichiometry must be finite numbers")
        if not self.source_id or not self.source_version or not self.source_record_id:
            raise ValueError("candidate provenance is incomplete")
        if self.chemical_status not in _CHEMICAL_STATUSES:
            raise ValueError("unknown chemical evidence status")
        if self.enzyme_status not in _ENZYME_STATUSES:
            raise ValueError("unknown enzyme evidence status")
        if self.experimental_status != "unknown":
            raise ValueError("experimental status is unknown until measured")
        if self.model_feasibility not in _MODEL_STATUSES:
            raise ValueError("unknown model feasibility status")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PathwayPatchDryRun:
    patch_id: str
    base_model_sha256: str
    candidate_ids: tuple[str, ...]
    proposed_reaction_ids: tuple[str, ...]
    rejected: tuple[dict[str, str], ...] = ()
    status: str = "PROPOSED"
    accepted: bool = False
    source_model_unchanged: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ApprovedPathwayPatch:
    patch_id: str
    approval_id: str
    candidate_ids: tuple[str, ...]
    status: str = "APPROVED_FOR_COPY_ANALYSIS"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceAuditEvent:
    request_id: str
    event: str
    output_sha256: str | None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class EvidenceAuditLog:
    """Append-only local audit for duplicate and contradictory evidence."""

    def __init__(self) -> None:
        self._seen: dict[str, str | None] = {}
        self._events: list[EvidenceAuditEvent] = []

    def record(self, result: EvidenceResult) -> EvidenceAuditEvent:
        previous = self._seen.get(result.request_id, _MISSING)
        if previous is _MISSING:
            event = "recorded"
        elif previous == result.output_sha256:
            event = "duplicate"
        else:
            event = "contradictory"
        if result.request_id not in self._seen:
            self._seen[result.request_id] = result.output_sha256
        audit = EvidenceAuditEvent(
            result.request_id,
            event,
            result.output_sha256,
            result.reason,
        )
        self._events.append(audit)
        return audit

    @property
    def events(self) -> tuple[EvidenceAuditEvent, ...]:
        return tuple(self._events)


class PathwaySandbox:
    """Create candidate patches without modifying source or accepted pointers."""

    def __init__(self) -> None:
        self._candidates: dict[str, PathwayCandidate] = {}
        self._patches: dict[str, PathwayPatchDryRun] = {}
        self._revoked: set[str] = set()
        self._events: list[dict[str, str]] = []
        self.evidence_audit = EvidenceAuditLog()

    def add_candidate(self, candidate: PathwayCandidate) -> str:
        if candidate.candidate_id in self._candidates:
            self._events.append(
                {"event": "duplicate_candidate", "candidate_id": candidate.candidate_id}
            )
            return "duplicate"
        self._candidates[candidate.candidate_id] = candidate
        self._events.append(
            {"event": "candidate_recorded", "candidate_id": candidate.candidate_id}
        )
        return "recorded"

    def record_evidence(self, result: EvidenceResult) -> EvidenceAuditEvent:
        return self.evidence_audit.record(result)

    def propose(self, model: Any, candidate_ids: tuple[str, ...]) -> PathwayPatchDryRun:
        if not candidate_ids:
            raise ValueError("at least one candidate is required")
        source_hash = model_fingerprint(model)
        rejected: list[dict[str, str]] = []
        accepted: list[PathwayCandidate] = []
        existing = {reaction.id for reaction in model.reactions}
        for candidate_id in candidate_ids:
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                rejected.append({"candidate_id": candidate_id, "reason": "unknown_candidate"})
            elif candidate.chemical_status != "verified":
                rejected.append(
                    {"candidate_id": candidate_id, "reason": "chemical_status_not_verified"}
                )
            elif candidate.enzyme_status != "supported":
                rejected.append(
                    {"candidate_id": candidate_id, "reason": "enzyme_support_not_verified"}
                )
            elif candidate.model_feasibility != "feasible":
                rejected.append(
                    {"candidate_id": candidate_id, "reason": "model_feasibility_not_verified"}
                )
            elif candidate.required_conditions:
                rejected.append(
                    {"candidate_id": candidate_id, "reason": "missing_declared_conditions"}
                )
            elif candidate.reaction_id in existing:
                rejected.append(
                    {"candidate_id": candidate_id, "reason": "reaction_already_exists"}
                )
            else:
                accepted.append(candidate)
                existing.add(candidate.reaction_id)
        payload = {
            "base_model_sha256": source_hash,
            "candidate_ids": [item.candidate_id for item in accepted],
            "reaction_ids": [item.reaction_id for item in accepted],
            "rejected": rejected,
        }
        patch_id = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        patch = PathwayPatchDryRun(
            patch_id,
            source_hash,
            tuple(item.candidate_id for item in accepted),
            tuple(item.reaction_id for item in accepted),
            tuple(rejected),
        )
        self._patches[patch_id] = patch
        self._events.append({"event": "patch_proposed", "patch_id": patch_id})
        return patch

    def approve_patch(self, patch: PathwayPatchDryRun, approval_id: str) -> ApprovedPathwayPatch:
        if patch.patch_id not in self._patches or patch.patch_id in self._revoked:
            raise ValueError("patch is unknown or revoked")
        if not approval_id.strip():
            raise ValueError("explicit approval ID is required")
        self._events.append({"event": "patch_approved", "patch_id": patch.patch_id})
        return ApprovedPathwayPatch(patch.patch_id, approval_id, patch.candidate_ids)

    def analyze_approved(self, model: Any, approved: ApprovedPathwayPatch) -> dict[str, object]:
        patch = self._patches.get(approved.patch_id)
        if patch is None or approved.patch_id in self._revoked:
            raise ValueError("patch is unknown or revoked")
        if approved.status != "APPROVED_FOR_COPY_ANALYSIS":
            raise ValueError("patch approval is required")
        if model_fingerprint(model) != patch.base_model_sha256:
            raise ValueError("source model changed after dry-run")
        work = model.copy()
        for candidate_id in approved.candidate_ids:
            _add_candidate_reaction(work, self._candidates[candidate_id])
        candidate_hash = model_fingerprint(work)
        self._events.append({"event": "candidate_copy_analyzed", "patch_id": patch.patch_id})
        return {
            "status": "candidate_copy_analyzed",
            "track": "pathway_candidate_sandbox",
            "patch_id": patch.patch_id,
            "approval_id": approved.approval_id,
            "base_model_sha256": patch.base_model_sha256,
            "candidate_model_sha256": candidate_hash,
            "source_model_unchanged": model_fingerprint(model) == patch.base_model_sha256,
            "accepted_pointer_changed": False,
            "experimental_status": "unknown",
        }

    def revoke(self, patch: PathwayPatchDryRun) -> dict[str, object]:
        self._revoked.add(patch.patch_id)
        self._events.append({"event": "patch_revoked", "patch_id": patch.patch_id})
        return {
            "patch_id": patch.patch_id,
            "status": "REVOKED",
            "source_model_unchanged": True,
            "accepted_pointer_changed": False,
        }

    @property
    def events(self) -> tuple[dict[str, str], ...]:
        return tuple(self._events)


def _add_candidate_reaction(model: Any, candidate: PathwayCandidate) -> None:
    from cobra import Metabolite, Reaction

    reaction = Reaction(candidate.reaction_id)
    metabolites = {}
    for metabolite_id, coefficient in candidate.stoichiometry.items():
        if metabolite_id in model.metabolites:
            metabolite = model.metabolites.get_by_id(metabolite_id)
        else:
            compartment = metabolite_id.rsplit("_", 1)[-1] if "_" in metabolite_id else "c"
            metabolite = Metabolite(metabolite_id, compartment=compartment)
        metabolites[metabolite] = float(coefficient)
    reaction.add_metabolites(metabolites)
    reaction.gene_reaction_rule = candidate.gpr
    model.add_reactions([reaction])
