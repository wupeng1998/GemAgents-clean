"""Resolve benchmark identities from explicit, hash-bound mapping evidence.

Workbook names and metrics are inventory data. They never resolve strain or
reference identity. Preparing a mapping does not execute a reconstruction.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 2


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate mapping key: {key}")
        result[key] = value
    return result


def load_explicit_mapping(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("explicit mapping requires schema_version=2 and records")
    records = raw.get("records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError("explicit mapping records must be a list of objects")
    return records


def validate_prepared_mapping(payload: dict) -> list[dict]:
    """Reject legacy or internally inconsistent mappings before any model is read."""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Batch execution requires explicit mapping schema_version=2")
    policy = payload.get("policy")
    if not isinstance(policy, dict) or policy.get("evaluation_track") != "reference_assisted":
        raise ValueError("Batch mapping must declare the reference_assisted evaluation track")
    builds = payload.get("builds")
    if not isinstance(builds, list) or not all(isinstance(row, dict) for row in builds):
        raise ValueError("Batch mapping builds must be a list of objects")
    for build in builds:
        if build.get("status") != "ready":
            continue
        references = build.get("references")
        candidates = build.get("candidate_reference_model_ids")
        if not isinstance(references, list) or not isinstance(candidates, list):
            raise ValueError("Ready build lacks explicit reference records")
        reference_ids = {row.get("model_id") for row in references if isinstance(row, dict)}
        selected = build.get("selected_reference_model_id")
        biomass = build.get("biomass_source_model_id")
        if (
            selected not in reference_ids
            or selected not in candidates
            or biomass not in reference_ids
            or not set(candidates).issubset(reference_ids)
        ):
            raise ValueError("Ready build has inconsistent explicit reference selections")
        if not re.fullmatch(r"GC[AF]_\d{9}\.\d+", str(build.get("sequence_accession"))):
            raise ValueError("Ready build requires an assembly accession with version")
        if not re.fullmatch(r"[0-9a-f]{64}", str(build.get("sequence_sha256"))):
            raise ValueError("Ready build requires a sequence SHA-256")
    return builds


def _evidence(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _resolve_record(sequence: dict, records: list[dict]) -> tuple[dict | None, list[str]]:
    # An accession identifies the assembly/version; its hash must independently
    # agree before any reference file is accessed.
    matches = [
        row
        for row in records
        if row.get("assembly_accession") == sequence.get("assembly_accession")
        and sequence.get("assembly_accession")
    ]
    if len(matches) != 1:
        return None, ["mapping_missing" if not matches else "mapping_ambiguous"]
    record = matches[0]
    reasons = []
    if not re.fullmatch(r"GC[AF]_\d{9}\.\d+", str(record.get("assembly_accession"))):
        reasons.append("invalid_assembly_accession_version")
    if not sequence.get("sequence") or not sequence.get("sequence_sha256"):
        reasons.append("sequence_missing_empty_or_ambiguous")
    if record.get("sequence_sha256") != sequence.get("sequence_sha256"):
        reasons.append("sequence_hash_mismatch")
    if not _evidence(record.get("strain_id")) or not _evidence(record.get("mapping_evidence")):
        reasons.append("strain_or_mapping_evidence_missing")
    if record.get("kingdom") not in {"bacteria", "archaea", "eukaryota"}:
        reasons.append("explicit_domain_missing_or_invalid")
    if not _evidence(record.get("taxonomy_evidence")):
        reasons.append("taxonomy_evidence_missing")
    return record, reasons


def _references(record: dict, model_directory: Path) -> tuple[list[dict], list[str]]:
    references = record.get("references")
    if not isinstance(references, list) or not references:
        return [], ["reference_records_missing"]
    result, reasons = [], []
    for reference in references:
        if not isinstance(reference, dict):
            reasons.append("invalid_reference_record")
            continue
        model_id = reference.get("model_id")
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", model_id):
            reasons.append("invalid_reference_model_id")
            continue
        path = model_directory / f"{model_id}.xml"
        digest = sha256_file(path)
        if digest is None:
            reasons.append(f"reference_model_missing:{model_id}")
        elif digest != reference.get("model_sha256"):
            reasons.append(f"reference_hash_mismatch:{model_id}")
        if not _evidence(reference.get("source_url")) or not _evidence(
            reference.get("mapping_evidence")
        ):
            reasons.append(f"reference_source_or_mapping_evidence_missing:{model_id}")
        result.append(
            {
                "model_id": model_id,
                "model": str(path.resolve()),
                "model_sha256": digest,
                "source_url": reference.get("source_url"),
                "mapping_evidence": reference.get("mapping_evidence"),
            }
        )
    ids = [row["model_id"] for row in result]
    if len(ids) != len(set(ids)):
        reasons.append("duplicate_reference_model_id")
    return sorted(result, key=lambda row: row["model_id"]), reasons


def build_mapping(
    sequences: list[dict],
    publications: list[dict],
    records: list[dict],
    *,
    model_directory: Path,
    workbook_sha256: str | None,
    explicit_mapping_sha256: str | None,
    medium_reader: Callable[[Path], dict[str, float]],
) -> dict:
    """Retain every workbook sequence, including unresolved and unsupported rows."""
    builds = []
    accessions = Counter(row.get("assembly_accession") for row in sequences)
    used_models = set()
    for sequence in sequences:
        record, reasons = _resolve_record(sequence, records)
        if accessions[sequence.get("assembly_accession")] > 1:
            reasons.append("duplicate_sequence_accession")
        references = []
        selected = biomass = None
        candidates = []
        if record is not None:
            # Read declared model files only after sequence identity is verified.
            if not reasons:
                references, reference_reasons = _references(record, model_directory)
                reasons.extend(reference_reasons)
            selected = record.get("selected_reference_model_id")
            biomass = record.get("biomass_source_model_id")
            candidates = record.get("candidate_reference_model_ids")
            ids = {ref["model_id"] for ref in references}
            if not selected or selected not in ids or not biomass or biomass not in ids:
                reasons.append("explicit_selected_or_biomass_reference_missing")
            if (
                not isinstance(candidates, list)
                or not candidates
                or any(not isinstance(value, str) for value in candidates)
                or len(candidates) != len(set(candidates))
                or not set(candidates).issubset(ids)
                or selected not in candidates
            ):
                reasons.append("explicit_candidate_references_invalid")
                candidates = []
        status = "BLOCKED_MAPPING" if reasons else "ready"
        if not reasons and record["kingdom"] == "eukaryota":
            status = "unsupported_eukaryotic_frontend"
        sequence_id = sequence.get("assembly_accession")
        if not sequence_id or accessions[sequence_id] != 1:
            sequence_id = f"sequence_row_{sequence['excel_row']}"
        build = {
            "id": sequence_id,
            "status": status,
            "mapping_status": "BLOCKED_MAPPING" if reasons else "mapped",
            "mapping_reasons": reasons,
            "sequence_excel_name": sequence.get("excel_name"),
            "sequence_excel_row": sequence["excel_row"],
            "sequence": sequence.get("sequence"),
            "sequence_sha256": sequence.get("sequence_sha256"),
            "sequence_accession": sequence.get("assembly_accession"),
            "strain_id": (record or {}).get("strain_id"),
            "organism": sequence.get("organism"),
            "kingdom": (record or {}).get("kingdom"),
            "taxonomy_evidence": (record or {}).get("taxonomy_evidence"),
            "biomass_template": biomass,
            "biomass_source_model_id": biomass,
            "selected_reference_model_id": selected,
            "candidate_reference_model_ids": sorted(candidates),
            "references": references,
            "mapping_evidence": (record or {}).get("mapping_evidence"),
            "benchmark_medium": None,
            "benchmark_medium_source": None,
            "preparation_data_access": [],
            "effective_data_use": {
                "evaluation_track": "reference_assisted",
                "candidate_access": [],
                "selected_use": {},
                "sequence_only_claim_allowed": False,
            },
        }
        if not reasons:
            used_models.update(row["model_id"] for row in references)
        if status == "ready":
            medium_reference = next(row for row in references if row["model_id"] == selected)
            build["benchmark_medium"] = medium_reader(Path(medium_reference["model"]))
            build["benchmark_medium_source"] = medium_reference["model"]
            build["preparation_data_access"] = [
                {
                    "model_id": selected,
                    "model_sha256": medium_reference["model_sha256"],
                    "purpose": "published_medium_extraction",
                    "access_scope": "full_SBML_deserialized",
                }
            ]
            build["effective_data_use"] = {
                "evaluation_track": "reference_assisted",
                "candidate_access": [row["model_id"] for row in references],
                "selected_use": {
                    "published_medium": selected,
                    "biomass_source": biomass,
                    "declared_reconstruction_candidates": sorted(candidates),
                },
                "sequence_only_claim_allowed": False,
            }
        builds.append(build)
    unresolved_publications = [
        {**row, "status": "BLOCKED_MAPPING", "reason": "no_verified_sequence_reference_mapping"}
        for row in publications
        if row.get("model_id") not in used_models
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "workbook_sha256": workbook_sha256,
        "explicit_mapping_sha256": explicit_mapping_sha256,
        "policy": {
            "evaluation_track": "reference_assisted",
            "allowed_uses": {
                "reactions": True,
                "gpr": True,
                "biomass": True,
                "medium": True,
                "reference_derived_tasks": True,
                "calibration": False,
            },
            "execution_status": "NOT_RUN",
            "candidate_access": "NOT_RUN; allowed model IDs are declared per build",
            "selected_use": "NOT_RUN; determine from reconstruction evidence ledger",
            "independence_claim": "reference_assisted; no sequence-only accuracy claim",
            "historical_mapping_policy": "historical_policy_unverified; originals preserved",
        },
        "counts": {
            "excel_sequences": len(sequences),
            "excel_publications": len(publications),
            "mapping_records": len(records),
            "build_rows": len(builds),
            "ready_builds": sum(row["status"] == "ready" for row in builds),
            "blocked_mapping_builds": sum(row["status"] == "BLOCKED_MAPPING" for row in builds),
            "unsupported_eukaryotic": sum(
                row["status"] == "unsupported_eukaryotic_frontend" for row in builds
            ),
            "unresolved_publications": len(unresolved_publications),
        },
        "builds": sorted(builds, key=lambda row: row["id"]),
        "unresolved_publications": unresolved_publications,
        "unmatched_mapping_records": [
            row for row in records if row.get("assembly_accession") not in accessions
        ],
    }


def write_mapping(payload: dict, output_directory: Path) -> dict[str, str]:
    """Write new versioned artifacts, leaving all existing files untouched."""
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(encoded).hexdigest()
    stem = f"mapping.v2.{digest}"
    ready = [row for row in payload["builds"] if row["status"] == "ready"]
    biomass_sources = {}
    for build in ready:
        model_id = build["biomass_source_model_id"]
        reference = next(row for row in build["references"] if row["model_id"] == model_id)
        source = {
            "id": model_id,
            "model": reference["model"],
            "source_url": reference["source_url"],
            "model_sha256": reference["model_sha256"],
            "organism": build["organism"],
            "kingdom": build["kingdom"],
            "gram": "unspecified",
        }
        if (
            model_id in biomass_sources
            and biomass_sources[model_id]["kingdom"] != source["kingdom"]
        ):
            raise ValueError(f"conflicting biomass model taxonomy: {model_id}")
        biomass_sources[model_id] = source
    outputs = {
        "mapping": output_directory / f"{stem}.json",
        "biomass_sources": output_directory / f"{stem}.biomass-sources.json",
        "tasks": output_directory / f"{stem}.tasks.tsv",
        "hash": output_directory / f"{stem}.sha256",
    }
    if any(path.exists() for path in outputs.values()):
        raise FileExistsError("mapping artifacts already exist; choose a fresh output directory")
    output_directory.mkdir(parents=True, exist_ok=True)
    with outputs["mapping"].open("xb") as handle:
        handle.write(encoded)
    with outputs["biomass_sources"].open("x", encoding="utf-8") as handle:
        json.dump(
            {"schema_version": SCHEMA_VERSION, "sources": list(biomass_sources.values())},
            handle,
            indent=2,
        )
        handle.write("\n")
    with outputs["tasks"].open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["build_id", "status", "sequence", "reference_models"],
            delimiter="\t",
        )
        writer.writeheader()
        for build in payload["builds"]:
            writer.writerow(
                {
                    "build_id": build["id"],
                    "status": build["status"],
                    "sequence": build["sequence"] or "",
                    "reference_models": ",".join(build["candidate_reference_model_ids"]),
                }
            )
    with outputs["hash"].open("x", encoding="utf-8") as handle:
        handle.write(f"{digest}  {outputs['mapping'].name}\n")
    return {key: str(value) for key, value in outputs.items()}
