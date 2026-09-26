#!/usr/bin/env python3
"""Apply the audited operational chemistry/direction policy to a library view.

The full union remains source evidence.  This command updates only strict
``universe.xml.gz`` and its row-level metadata; reconstruction also applies
the same policy at load time for reproducibility.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import tempfile
from pathlib import Path

from cobra.io import read_sbml_model, write_sbml_model

from GemAgents.metabolic.library.quality import (
    apply_canonical_chemistry,
    guard_energy_hydrolysis_direction,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path):
    with gzip.open(path, "rb") as source, tempfile.NamedTemporaryFile(suffix=".xml") as target:
        target.write(source.read())
        target.flush()
        return read_sbml_model(target.name)


def write(path: Path, model) -> None:
    with tempfile.NamedTemporaryFile(suffix=".xml") as target:
        write_sbml_model(model, target.name)
        target.flush()
        with gzip.open(path, "wb") as compressed, open(target.name, "rb") as source:
            compressed.write(source.read())


def apply(library: Path) -> dict[str, object]:
    model_path = library / "universe.xml.gz"
    model = load(model_path)
    chemistry = apply_canonical_chemistry(model)
    guards = []
    for reaction in model.reactions:
        before = list(reaction.bounds)
        if guard_energy_hydrolysis_direction(reaction):
            guards.append(
                {
                    "reaction_id": reaction.id,
                    "before_bounds": before,
                    "after_bounds": list(reaction.bounds),
                    "reason": reaction.notes.get("qc_direction_guard", "unspecified"),
                }
            )
    # A second invocation should preserve the already recorded guard set.
    for reaction in model.reactions:
        if reaction.notes.get("qc_direction_guard") and reaction.id not in {
            row["reaction_id"] for row in guards
        }:
            guards.append(
                {
                    "reaction_id": reaction.id,
                    "before_bounds": None,
                    "after_bounds": list(reaction.bounds),
                    "reason": reaction.notes["qc_direction_guard"],
                }
            )
    write(model_path, model)

    quality_path = library / "reaction_quality.tsv"
    if quality_path.is_file():
        with quality_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        changed = {row["reaction_id"]: row for row in guards}
        for row in rows:
            correction = changed.get(row["reaction_id"])
            if correction:
                row["lower_bound"], row["upper_bound"] = map(str, correction["after_bounds"])
                lower, upper = correction["after_bounds"]
                row["direction"] = (
                    "reversible" if lower < 0 < upper else "forward" if upper > 0 else "reverse"
                )
        with quality_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        quality_json_path = library / "reaction_quality.json"
        if quality_json_path.is_file():
            quality_json = json.loads(quality_json_path.read_text(encoding="utf-8"))
            from collections import Counter

            directions = Counter(row["direction"] for row in rows)
            quality_json.setdefault("counts", {}).update(
                {f"direction_{key}": value for key, value in directions.items()}
            )
            quality_json["energy_direction_guards"] = [row["reaction_id"] for row in guards]
            quality_json["canonical_chemistry_corrections"] = chemistry
            quality_json_path.write_text(
                json.dumps(quality_json, ensure_ascii=False, indent=2) + "\n"
            )

    reactions_tsv = library / "reactions.tsv"
    if reactions_tsv.is_file():
        with reactions_tsv.open(encoding="utf-8", newline="") as handle:
            reaction_rows = list(csv.DictReader(handle, delimiter="\t"))
        changed = {row["reaction_id"]: row for row in guards}
        for row in reaction_rows:
            correction = changed.get(row["id"])
            if correction:
                row["lower_bound"], row["upper_bound"] = map(str, correction["after_bounds"])
        with reactions_tsv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=reaction_rows[0].keys(), delimiter="\t")
            writer.writeheader()
            writer.writerows(reaction_rows)

    manifest_path = library / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_sha256"] = sha256(model_path)
    manifest["canonical_chemistry_corrections"] = chemistry
    manifest["energy_direction_guards"] = [row["reaction_id"] for row in guards]
    if quality_path.is_file():
        with quality_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        from collections import Counter

        manifest.setdefault("quality_control", {}).setdefault("counts", {}).update(
            {
                f"direction_{key}": value
                for key, value in Counter(row["direction"] for row in rows).items()
            }
        )
    manifest.setdefault("qc_feedback", {})["operational_policy"] = {
        "canonical_chemistry_corrections": chemistry,
        "energy_direction_guards": guards,
        "full_union_unchanged": True,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"library": str(library), "chemistry": chemistry, "guards": guards}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    args = parser.parse_args()
    result = apply(args.library)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
