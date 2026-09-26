#!/usr/bin/env python3
"""Build explicit, hash-bound sequence-to-BiGG mapping from the benchmark workbook.

This keeps the workbook's published model suffixes as auditable evidence while
leaving ambiguous multi-model matches as candidate sets with a deterministic
primary (the first published row in workbook order).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def accession(value: str) -> str | None:
    match = re.search(r"(GC[AF])[_ ]0*(\d+)\.(\d+)\b", value)
    if not match:
        return None
    return f"{match.group(1)}_{int(match.group(2)):09d}.{int(match.group(3))}"


def organism(value: str) -> str:
    return re.sub(r"\s+GC[AF][ _]\d+\.\d+\.faa$", "", value, flags=re.I).removesuffix(".faa")


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("&#x27;", "'"))


def model_split(value: str, model_ids: set[str]) -> tuple[str, str] | None:
    hits = [model_id for model_id in model_ids if value.endswith("_" + model_id)]
    if len(hits) != 1:
        return None
    model_id = hits[0]
    return value[: -len(model_id) - 1], model_id


def kingdom(name: str) -> str:
    lowered = name.lower()
    if any(
        token in lowered
        for token in (
            "plasmod",
            "saccharomyces",
            "chlamydomonas",
            "cricetulus",
            "phaeodactylum",
            "trypanosoma",
        )
    ):
        return "eukaryota"
    if "methanosarcina" in lowered:
        return "archaea"
    return "bacteria"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--sequence-directory", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from GemAgents.metabolic.jobs.bigg_inventory import read_inventory_rows

    model_paths = {path.stem: path for path in args.model_directory.glob("*.xml")}
    rows = [
        (row_number, row)
        for row_number, row in enumerate(read_inventory_rows(args.workbook), 2)
    ]

    sequence_paths = {
        path.name: path for path in args.sequence_directory.iterdir() if path.is_file()
    }
    published_by_organism: dict[str, list[tuple[int, str, str]]] = {}
    for row_number, row in rows:
        if row.get("model_type") != "Published":
            continue
        split = model_split(str(row["fasta_name"]), set(model_paths))
        if split is None:
            continue
        published_name, model_id = split
        published_by_organism.setdefault(normalize(published_name), []).append(
            (row_number, model_id, str(row["fasta_name"]))
        )

    records = []
    for row_number, row in rows:
        if row.get("model_type") != "CarveMe":
            continue
        excel_name = str(row["fasta_name"])
        assembly = accession(excel_name)
        if assembly is None:
            continue
        sequence = next(
            (path for path in sequence_paths.values() if accession(path.name) == assembly),
            None,
        )
        org = organism(excel_name)
        matches = published_by_organism.get(normalize(org), [])
        # Known public naming variants in the workbook.
        if not matches and normalize(org) == normalize("Methanosarcina barkeri MS"):
            matches = published_by_organism.get(normalize("Methanosarcina barkeri str. Fusaro"), [])
        if not matches and normalize(org) == normalize("Plasmodium berghei ANKA"):
            matches = published_by_organism.get(normalize("Plasmodium berghei"), [])
        references = []
        for source_row, model_id, published_name in matches:
            path = model_paths[model_id]
            references.append(
                {
                    "model_id": model_id,
                    "model_sha256": sha256(path),
                    "source_url": f"https://bigg.ucsd.edu/models/{model_id}",
                    "mapping_evidence": (
                        f"Workbook 原始数据 row {source_row} published entry {published_name}; "
                        f"organism label matched to {org}."
                    ),
                }
            )
        # Deduplicate repeated workbook rows while retaining original order.
        deduped = []
        seen = set()
        for ref in references:
            if ref["model_id"] not in seen:
                deduped.append(ref)
                seen.add(ref["model_id"])
        records.append(
            {
                "assembly_accession": assembly,
                "sequence_sha256": sha256(sequence) if sequence is not None else None,
                "strain_id": org,
                "kingdom": kingdom(org),
                "taxonomy_evidence": (
                    "Local benchmark workbook organism label and assembly accession "
                    f"{assembly}."
                ),
                "mapping_evidence": (
                    f"Workbook 原始数据 row {row_number}; CarveMe sequence {excel_name}; "
                    "published BiGG suffixes are resolved from the same workbook."
                ),
                "selected_reference_model_id": deduped[0]["model_id"] if deduped else None,
                "biomass_source_model_id": deduped[0]["model_id"] if deduped else None,
                "candidate_reference_model_ids": [ref["model_id"] for ref in deduped],
                "references": deduped,
            }
        )

    payload = {"schema_version": 2, "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"records": len(records), "mapped": sum(bool(r["references"]) for r in records)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
