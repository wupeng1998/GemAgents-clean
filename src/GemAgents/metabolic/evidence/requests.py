"""Budgeted, reviewable evidence acquisition requests.

This module only produces evidence candidates and immutable request records.
It never edits a model or GPR; a later deterministic rebuild and independent
certificate are required before a candidate can affect a reconstruction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass

from GemAgents.datetime_compat import StrEnum
from GemAgents.provenance import restricted_path
from GemAgents.run_cache import stable_key

GAP_TYPES = frozenset(
    {
        "missing_subunit",
        "reaction_direction",
        "substrate_mapping",
        "compartment_chemistry",
        "biomass_mismatch",
    }
)


class AcquisitionStrategy(StrEnum):
    NO_EVIDENCE = "no_evidence"
    FIXED_ORDER = "fixed_order"
    SELECTIVE = "selective"


@dataclass(frozen=True)
class EvidenceRequest:
    request_id: str
    gap_type: str
    target: str
    query: str
    source_allowlist: tuple[str, ...]
    budget_units: int = 1

    @classmethod
    def create(
        cls,
        gap_type: str,
        target: str,
        query: str,
        *,
        source_allowlist: tuple[str, ...] = (),
        budget_units: int = 1,
    ) -> EvidenceRequest:
        if gap_type not in GAP_TYPES:
            raise ValueError(f"unsupported evidence gap: {gap_type}")
        if not target.strip() or not query.strip():
            raise ValueError("evidence requests require a target and query")
        if budget_units < 1:
            raise ValueError("budget_units must be positive")
        if any(restricted_path(source) for source in source_allowlist):
            raise ValueError("restricted evidence source is not allowed")
        payload = {
            "gap_type": gap_type,
            "target": target.strip(),
            "query": query.strip(),
            "source_allowlist": tuple(sorted(source_allowlist)),
            "budget_units": budget_units,
        }
        return cls(stable_key(payload), **payload)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceResult:
    request_id: str
    status: str
    source_id: str | None = None
    source_version: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None
    evidence: dict[str, object] | None = None
    reason: str = ""
    cost_units: int = 0
    rebuild_required: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceRevision:
    revision_id: str
    request_id: str
    links: tuple[dict[str, object], ...]
    status: str = "candidate_reviewed"
    rebuild_required: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Adapter = Callable[[EvidenceRequest], dict[str, object] | None]


class EvidenceBroker:
    """Execute authorized adapters under a finite budget and immutable ledger."""

    def __init__(
        self,
        adapters: dict[str, Adapter],
        *,
        authorized_sources: dict[str, str],
        budget_units: int = 3,
        strategy: AcquisitionStrategy = AcquisitionStrategy.SELECTIVE,
    ) -> None:
        if budget_units < 0:
            raise ValueError("budget_units must be nonnegative")
        if any(restricted_path(source) for source in authorized_sources):
            raise ValueError("restricted evidence source cannot be authorized here")
        self.adapters = dict(adapters)
        self.authorized_sources = dict(authorized_sources)
        self.remaining_units = budget_units
        self.strategy = AcquisitionStrategy(strategy)
        self.results: dict[str, EvidenceResult] = {}

    def acquire(self, request: EvidenceRequest, adapter_name: str) -> EvidenceResult:
        if request.request_id in self.results:
            return self.results[request.request_id]
        if self.strategy is AcquisitionStrategy.NO_EVIDENCE:
            return self._store(
                EvidenceResult(request.request_id, "rejected", reason="strategy_disabled")
            )
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return self._store(
                EvidenceResult(request.request_id, "rejected", reason="adapter_not_authorized")
            )
        if request.budget_units > self.remaining_units:
            return self._store(
                EvidenceResult(request.request_id, "rejected", reason="budget_exceeded")
            )
        if request.source_allowlist and adapter_name not in request.source_allowlist:
            return self._store(
                EvidenceResult(request.request_id, "rejected", reason="source_not_allowed")
            )
        self.remaining_units -= request.budget_units
        try:
            payload = adapter(request)
        except Exception as error:
            return self._store(
                EvidenceResult(
                    request.request_id,
                    "rejected",
                    reason=f"adapter_failure:{type(error).__name__}",
                    cost_units=request.budget_units,
                )
            )
        if not payload:
            return self._store(
                EvidenceResult(
                    request.request_id,
                    "not_found",
                    source_id=adapter_name,
                    source_version=self.authorized_sources.get(adapter_name),
                    input_sha256=_hash(request.query),
                    cost_units=request.budget_units,
                )
            )
        source_id = str(payload.get("source_id", adapter_name))
        source_version = str(payload.get("source_version", ""))
        evidence = payload.get("evidence")
        if (
            source_id not in self.authorized_sources
            or self.authorized_sources[source_id] != source_version
            or not source_version
            or not isinstance(evidence, dict)
            or not evidence
        ):
            return self._store(
                EvidenceResult(
                    request.request_id,
                    "rejected",
                    source_id=source_id,
                    source_version=source_version or None,
                    input_sha256=_hash(request.query),
                    reason="incomplete_or_unauthorized_citation",
                    cost_units=request.budget_units,
                )
            )
        result = EvidenceResult(
            request.request_id,
            "candidate",
            source_id=source_id,
            source_version=source_version,
            input_sha256=_hash(request.query),
            output_sha256=_hash(evidence),
            evidence=dict(evidence),
            cost_units=request.budget_units,
        )
        return self._store(result)

    def approve(self, result: EvidenceResult) -> EvidenceRevision:
        if result.status != "candidate" or not result.evidence:
            raise ValueError("only a cited candidate can enter review")
        required = {
            "source_id",
            "source_version",
            "source_record_id",
            "function_id",
            "protein_id",
        }
        if not required <= set(result.evidence):
            raise ValueError("candidate evidence is missing structured citation fields")
        if (
            result.evidence["source_id"] != result.source_id
            or result.evidence["source_version"] != result.source_version
        ):
            raise ValueError("candidate citation does not match adapter result")
        links = (dict(result.evidence),)
        return EvidenceRevision(
            stable_key({"request_id": result.request_id, "links": links}),
            result.request_id,
            links,
        )

    def _store(self, result: EvidenceResult) -> EvidenceResult:
        self.results[result.request_id] = result
        return result


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def order_requests(
    requests: list[EvidenceRequest],
    strategy: AcquisitionStrategy,
) -> tuple[EvidenceRequest, ...]:
    strategy = AcquisitionStrategy(strategy)
    if strategy is AcquisitionStrategy.NO_EVIDENCE:
        return ()
    if strategy is AcquisitionStrategy.FIXED_ORDER:
        return tuple(sorted(requests, key=lambda item: (item.gap_type, item.target)))
    return tuple(
        sorted(
            requests,
            key=lambda item: (
                item.gap_type not in {"missing_subunit", "compartment_chemistry"},
                item.budget_units,
                item.target,
            ),
        )
    )
