"""Append-only, metadata-only evidence registry and claim report helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from GemAgents.datetime_compat import UTC, StrEnum
from GemAgents.metabolic.evidence.claims import ClaimRecord, validate_claim


class TrustLayer(StrEnum):
    PROJECT_RULE = "project_rule"
    USER_AUTHORIZATION = "user_authorization"
    TOOL_RESULT = "tool_result"
    PAPER_SUMMARY = "paper_summary"
    CANDIDATE_EVIDENCE = "candidate_evidence"
    EXTERNAL_VALIDATION = "external_validation"


_STATUSES = frozenset({"candidate", "accepted", "negative", "failed", "superseded"})


@dataclass(frozen=True)
class EvidenceRecord:
    record_id: str
    artifact_id: str
    artifact_sha256: str
    kind: str
    trust_layer: str
    status: str
    scope: str
    track: str
    locator: str
    source_version: str | None = None
    created_at: str = ""
    previous_hash: str | None = None
    record_hash: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _record_hash(record: dict[str, object], previous_hash: str | None) -> str:
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    payload["previous_hash"] = previous_hash
    return hashlib.sha256(_canonical(payload)).hexdigest()


class EvidenceRegistry:
    """Store only structured evidence metadata; writes are append-only and chained."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("evidence registry must not be a symlink")

    def _read(self) -> list[EvidenceRecord]:
        if not self.path.exists():
            return []
        records: list[EvidenceRecord] = []
        previous_hash: str | None = None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            record = EvidenceRecord(**payload)
            if record.previous_hash != previous_hash or record.record_hash != _record_hash(
                payload, previous_hash
            ):
                raise ValueError("evidence registry hash chain is invalid")
            records.append(record)
            previous_hash = record.record_hash
        return records

    def verify(self) -> bool:
        self._read()
        return True

    def register(
        self,
        *,
        record_id: str,
        artifact_id: str,
        artifact: Path,
        kind: str,
        trust_layer: TrustLayer | str,
        status: str,
        scope: str,
        track: str,
        locator: str,
        source_version: str | None = None,
    ) -> EvidenceRecord:
        if not record_id.strip() or not artifact_id.strip() or not kind.strip():
            raise ValueError("evidence records require identifiers and kind")
        if status not in _STATUSES:
            raise ValueError(f"unsupported evidence status: {status}")
        if not artifact.is_file() or artifact.is_symlink():
            raise FileNotFoundError(artifact)
        layer = TrustLayer(trust_layer).value
        artifact_hash = _sha256(artifact)
        existing = self._read()
        for record in existing:
            if record.record_id == record_id:
                expected = {
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact_hash,
                    "kind": kind,
                    "trust_layer": layer,
                    "status": status,
                    "scope": scope,
                    "track": track,
                    "locator": locator,
                    "source_version": source_version,
                }
                if any(record.as_dict()[key] != value for key, value in expected.items()):
                    raise ValueError("record_id already exists with different evidence")
                return record
        previous_hash = existing[-1].record_hash if existing else None
        payload = {
            "record_id": record_id,
            "artifact_id": artifact_id,
            "artifact_sha256": artifact_hash,
            "kind": kind,
            "trust_layer": layer,
            "status": status,
            "scope": scope,
            "track": track,
            "locator": locator,
            "source_version": source_version,
            "created_at": datetime.now(UTC).isoformat(),
            "previous_hash": previous_hash,
        }
        payload["record_hash"] = _record_hash(payload, previous_hash)
        record = EvidenceRecord(**payload)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def query(
        self,
        *,
        artifact_id: str | None = None,
        trust_layer: TrustLayer | str | None = None,
        status: str | None = None,
        track: str | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        records: Iterable[EvidenceRecord] = self._read()
        if artifact_id is not None:
            records = (record for record in records if record.artifact_id == artifact_id)
        if trust_layer is not None:
            layer = TrustLayer(trust_layer).value
            records = (record for record in records if record.trust_layer == layer)
        if status is not None:
            records = (record for record in records if record.status == status)
        if track is not None:
            records = (record for record in records if record.track == track)
        return tuple(records)


def build_claim_report(
    claims: Iterable[ClaimRecord],
    registry: EvidenceRegistry,
    artifacts: dict[str, Path],
) -> dict[str, object]:
    """Build a deterministic structured report before any prose is produced."""
    rows: list[dict[str, object]] = []
    for claim in claims:
        artifact = artifacts.get(claim.artifact_id)
        validated = validate_claim(claim, artifact) if artifact is not None else claim
        if artifact is None:
            artifact_status = "missing"
        else:
            artifact_status = validated.validation_status
        records = registry.query(artifact_id=claim.artifact_id)
        accepted = [record for record in records if record.status == "accepted"]
        conflicting = len({record.status for record in records}) > 1
        promotable = len(accepted) == 1 and accepted[0].trust_layer != TrustLayer.CANDIDATE_EVIDENCE
        eligible = artifact_status == "verified" and promotable and not conflicting
        rows.append(
            {
                "claim_id": claim.claim_id,
                "scope": claim.scope,
                "artifact_id": claim.artifact_id,
                "artifact_sha256": claim.artifact_sha256,
                "evidence_location": claim.evidence_location,
                "condition": claim.condition,
                "validation_status": artifact_status,
                "registry_status": accepted[0].status if len(accepted) == 1 else "insufficient",
                "trust_layer": accepted[0].trust_layer if len(accepted) == 1 else None,
                "eligible": eligible,
                "reason": (
                    "verified"
                    if eligible
                    else "missing_or_stale_artifact"
                    if artifact_status != "verified"
                    else "candidate_trust_layer"
                    if accepted and not promotable
                    else "conflicting_or_unaccepted_evidence"
                ),
            }
        )
    return {
        "schema_version": 1,
        "claims": rows,
        "eligible_claims": sum(bool(row["eligible"]) for row in rows),
        "total_claims": len(rows),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
