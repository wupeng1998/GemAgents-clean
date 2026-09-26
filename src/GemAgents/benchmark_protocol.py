"""Frozen benchmark protocol validation; missing data stays missing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

TRACKS = {"de_novo_public", "biomass_controlled", "reference_assisted"}
REQUIRED_METRIC_FIELDS = {"positive_definition", "denominator", "missing_policy", "threshold"}


def load_protocol(path: Path) -> dict:
    # JSON is valid YAML 1.2, keeping the frozen protocol dependency free.
    protocol = json.loads(path.read_text(encoding="utf-8"))
    validate_protocol(protocol)
    return protocol


def validate_protocol(protocol: dict) -> None:
    if protocol.get("protocol_version") != "1.0.0":
        raise ValueError("Only benchmark protocol version 1.0.0 is frozen")
    tracks = {track["id"] for track in protocol.get("tracks", [])}
    if tracks != TRACKS:
        raise ValueError("Protocol must define exactly the three public evaluation tracks")
    for metric in protocol.get("metrics", []):
        missing = REQUIRED_METRIC_FIELDS - metric.keys()
        if missing:
            raise ValueError(f"Metric {metric.get('id')} is missing {sorted(missing)}")
        if metric["missing_policy"] not in {"exclude_from_denominator", "report_missing"}:
            raise ValueError("Missing labels may not be converted to a negative label")
    if protocol.get("test_policy", {}).get("protocol_hash"):
        expected = protocol["test_policy"]["protocol_hash"]
        if expected != protocol_hash(protocol, omit_hash=True):
            raise ValueError("Frozen protocol hash does not match protocol content")


def protocol_hash(protocol: dict, *, omit_hash: bool = False) -> str:
    payload = json.loads(json.dumps(protocol, sort_keys=True))
    if omit_hash:
        payload.get("test_policy", {}).pop("protocol_hash", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def validate_dataset_lock(lock: dict) -> None:
    for record in lock.get("records", []):
        required = {"sample_id", "source", "source_hash", "label_status", "exposure"}
        missing = required - record.keys()
        if missing:
            raise ValueError(f"Dataset record is missing {sorted(missing)}")
        if record["label_status"] not in {"measured", "missing", "unresolved"}:
            raise ValueError("Unmeasured phenotype cannot be encoded as a negative")
        if record["exposure"] not in {"unexposed", "exposed", "exposure_unknown"}:
            raise ValueError("Unknown reference exposure must remain explicit")


def validate_splits(splits: dict) -> None:
    groups = {}
    for split_name, records in splits.get("splits", {}).items():
        for record in records:
            sample_id = record["sample_id"]
            lineage = record.get("lineage_group", sample_id)
            if sample_id in groups and groups[sample_id] != split_name:
                raise ValueError(f"Sample leakage across splits: {sample_id}")
            groups[sample_id] = split_name
            record["lineage_group"] = lineage
    split_groups = {}
    for split_name, records in splits.get("splits", {}).items():
        for record in records:
            group = record["lineage_group"]
            if group in split_groups and split_groups[group] != split_name:
                raise ValueError(f"Lineage leakage across splits: {group}")
            split_groups[group] = split_name

