"""Structured claims that keep reports tied to immutable artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    scope: str
    artifact_id: str
    artifact_sha256: str
    evidence_location: str
    condition: str
    validation_status: str = "unverified"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def claim_for_artifact(
    claim_id: str,
    scope: str,
    artifact_id: str,
    artifact: Path,
    evidence_location: str,
    condition: str,
    *,
    validation_status: str = "verified",
) -> ClaimRecord:
    if not artifact.is_file():
        raise FileNotFoundError(artifact)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return ClaimRecord(
        claim_id=claim_id,
        scope=scope,
        artifact_id=artifact_id,
        artifact_sha256=digest,
        evidence_location=evidence_location,
        condition=condition,
        validation_status=validation_status,
    )


def validate_claim(claim: ClaimRecord, artifact: Path) -> ClaimRecord:
    if not artifact.is_file():
        return ClaimRecord(**{**claim.as_dict(), "validation_status": "missing"})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    status = "verified" if digest == claim.artifact_sha256 else "stale"
    return ClaimRecord(**{**claim.as_dict(), "validation_status": status})

