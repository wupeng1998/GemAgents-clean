"""Explicit source records used by reconstruction manifests."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from GemAgents.datetime_compat import UTC

RESTRICTED = re.compile(r"(?:^|[\\/_.-])(mqc|pear)(?:$|[\\/_.-])", re.I)


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    source_class: str
    source_path: str
    content_sha256: str | None
    source_hash: str | None
    acquisition_time: str | None
    license_or_usage_terms: str
    authorization_reference: str | None
    derived_from: tuple[str, ...]
    allowed_tracks: tuple[str, ...]
    status: str


def source_record(
    path: str | Path,
    *,
    source_id: str | None = None,
    source_class: str = "unknown",
    license_or_usage_terms: str = "unknown",
    authorization_reference: str | None = None,
    derived_from: tuple[str, ...] = (),
    allowed_tracks: tuple[str, ...] = ("reference_assisted",),
) -> SourceRecord:
    target = Path(path)
    name = source_id or target.name
    if restricted_path(target):
        return SourceRecord(
            name,
            source_class,
            str(target),
            None,
            None,
            None,
            license_or_usage_terms,
            authorization_reference,
            derived_from,
            (),
            "BLOCKED_POLICY",
        )
    if not target.is_file():
        return SourceRecord(
            name,
            source_class,
            str(target),
            None,
            None,
            None,
            license_or_usage_terms,
            authorization_reference,
            derived_from,
            allowed_tracks,
            "BLOCKED_ASSET",
        )
    hasher = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    acquired = datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat()
    status = "AUTHORIZED" if authorization_reference else "DECLARED_UNVERIFIED"
    return SourceRecord(
        name,
        source_class,
        str(target.resolve()),
        digest,
        digest,
        acquired,
        license_or_usage_terms,
        authorization_reference,
        derived_from,
        allowed_tracks,
        status,
    )


def ledger(paths: list[dict]) -> list[dict]:
    """Build a serializable ledger; one restricted alternate does not erase a public row."""
    result = []
    for item in paths:
        record = source_record(
            item["path"],
            source_id=item.get("source_id"),
            source_class=item.get("source_class", "unknown"),
            license_or_usage_terms=item.get("license_or_usage_terms", "unknown"),
            authorization_reference=item.get("authorization_reference"),
            derived_from=tuple(item.get("derived_from", ())),
            allowed_tracks=tuple(item.get("allowed_tracks", ("reference_assisted",))),
        )
        result.append(asdict(record))
    return result


def restricted_path(path: str | Path) -> bool:
    target = Path(path)
    return bool(RESTRICTED.search(str(target)) or RESTRICTED.search(str(target.resolve())))


def filter_source_records(record: dict) -> tuple[dict | None, list[dict]]:
    """Filter source identifiers, never arbitrary reaction labels or JSON text."""
    rejected = []
    identity_fields = ("source", "source_id", "source_model", "source_path")
    restricted = any(RESTRICTED.search(str(record.get(k, ""))) for k in identity_fields)
    if restricted:
        return None, [{"record": record, "status": "BLOCKED_POLICY"}]
    result = dict(record)
    if "alternate_sources" in record:
        result["alternate_sources"] = []
        for alternate in record["alternate_sources"]:
            accepted, excluded = filter_source_records(alternate)
            rejected.extend(excluded)
            if accepted is not None:
                result["alternate_sources"].append(accepted)
    return result, rejected
