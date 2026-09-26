#!/usr/bin/env python3
"""Create an auditable sequence-to-BiGG benchmark manifest from the workbook."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--sequence-directory", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--explicit-mapping", type=Path)
    return parser.parse_args()


def accession(value: str) -> tuple[str, int, int] | None:
    match = re.search(r"(GC[AF])[_ ]0*(\d+)\.(\d+)\b", value)
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def sequence_organism(value: str) -> str:
    value = re.sub(r"\s+GC[AF][ _]\d+\.\d+\.faa$", "", value, flags=re.I)
    return value.removesuffix(".faa")


def sha256_file(path: Path) -> str | None:
    if not path or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def published_model_id(value: str, model_ids: set[str]) -> str | None:
    matches = [model_id for model_id in model_ids if value.endswith("_" + model_id)]
    return matches[0] if len(matches) == 1 else None


def actual_sequence(excel_name: str, sequence_paths: list[Path]) -> Path | None:
    wanted_accession = accession(excel_name)
    if wanted_accession:
        hits = [path for path in sequence_paths if accession(path.name) == wanted_accession]
    else:
        return None
    hits = [path for path in hits if path.is_file() and path.stat().st_size > 0]
    return hits[0] if len(hits) == 1 else None


def published_default_medium(model_path: Path) -> dict[str, float]:
    """Extract only the declared uptake condition, not network or GPR evidence."""
    from cobra.io import read_sbml_model

    model = read_sbml_model(str(model_path))
    return {
        reaction_id: float(rate)
        for reaction_id, rate in sorted(model.medium.items())
        if float(rate) > 0
    }


def prepare(args: argparse.Namespace) -> dict:
    """Prepare a new mapping; workbook species names are descriptive only."""
    from GemAgents.batch_mapping import build_mapping, load_explicit_mapping, write_mapping
    from GemAgents.metabolic.jobs.bigg_inventory import read_inventory_rows

    model_ids = {path.stem for path in args.model_directory.glob("*.xml")}
    sequence_paths = sorted(args.sequence_directory.iterdir())
    rows = read_inventory_rows(args.workbook)
    sequences, publications = [], []
    for row_number, row in enumerate(rows, 2):
        name = str(row["fasta_name"])
        if row["model_type"] == "CarveMe":
            path = actual_sequence(name, sequence_paths)
            parsed = accession(name)
            sequences.append(
                {
                    "excel_row": row_number,
                    "excel_name": name,
                    "organism": sequence_organism(name),
                    "assembly_accession": (
                        f"{parsed[0]}_{parsed[1]:09d}.{parsed[2]}" if parsed else None
                    ),
                    "sequence": str(path.resolve()) if path else None,
                    "sequence_sha256": sha256_file(path),
                }
            )
        elif row["model_type"] == "Published":
            publications.append(
                {
                    "excel_row": row_number,
                    "excel_name": name,
                    "model_id": published_model_id(name, model_ids),
                    "published_metrics": {
                        key: row.get(key)
                        for key in (
                            "nadh",
                            "atp",
                            "biomass",
                            "reactions",
                            "metabolites",
                            "genes",
                            "memote",
                            "size(MB)",
                            "time(min)",
                        )
                    },
                }
            )
    records = []
    if args.explicit_mapping:
        records = load_explicit_mapping(args.explicit_mapping)
    payload = build_mapping(
        sequences,
        publications,
        records,
        model_directory=args.model_directory,
        workbook_sha256=sha256_file(args.workbook),
        explicit_mapping_sha256=sha256_file(args.explicit_mapping),
        medium_reader=published_default_medium,
    )
    paths = write_mapping(payload, args.output_directory)
    print(json.dumps({"counts": payload["counts"], "outputs": paths}, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    prepare(parse_args())


if __name__ == "__main__":
    main()
