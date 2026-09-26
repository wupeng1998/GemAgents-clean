#!/usr/bin/env python3
"""Create a hash-bound reaction-library view with local EC aliases.

Only reaction annotations are changed.  Stoichiometry, bounds, metabolites and
reaction IDs are copied from the parent library and remain unchanged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

MODEL_FILES = (
    "universe_full.xml.gz",
    "universe.xml.gz",
    "universe_bigg_full.xml.gz",
    "universe_bigg.xml.gz",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--ec-alias-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def copy_metadata(parent: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    for source in parent.iterdir():
        if source.name in {"manifest.json", *MODEL_FILES, "reactions.tsv", "ec-aliases.tsv"}:
            continue
        target = output / source.name
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False)
        else:
            shutil.copy2(source, target, follow_symlinks=True)


def write_reactions_table(parent: Path, output: Path, strict_model) -> None:
    by_id = {reaction.id: reaction for reaction in strict_model.reactions}
    with (parent / "reactions.tsv").open(encoding="utf-8") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    with (output / "reactions.tsv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for row in rows:
            reaction = by_id.get(row["id"])
            if reaction is not None:
                row["ec"] = json.dumps(reaction.annotation.get("ec-code", []))
            writer.writerow(row)


def main() -> None:
    args = parse_args()
    parent = args.library.resolve()
    alias_source = args.ec_alias_source.resolve()
    output = args.output.resolve()
    if not parent.is_dir():
        raise SystemExit(f"library does not exist: {parent}")
    if not alias_source.is_file():
        raise SystemExit(f"EC alias source does not exist: {alias_source}")
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    required = [parent / name for name in MODEL_FILES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"parent library is missing model files: {missing}")

    from cobra.io import read_sbml_model, write_sbml_model

    from GemAgents.metabolic.io import metabolic_hash
    from GemAgents.metabolic.library.ec_aliases import apply_ec_aliases

    copy_metadata(parent, output)
    alias_ledger: list[dict[str, str]] = []
    model_hashes: dict[str, str] = {}
    alias_stats = {"rows": 0, "matched": 0, "unmatched": 0}
    strict_model = None
    for name in MODEL_FILES:
        model = read_sbml_model(str(parent / name))
        ledger, stats = apply_ec_aliases(model, [alias_source])
        if name == "universe.xml.gz":
            strict_model = model
            alias_ledger = ledger
            alias_stats = stats
        destination = output / name
        write_sbml_model(model, str(destination))
        model_hashes[name] = metabolic_hash(destination)
    if strict_model is None:
        raise RuntimeError("strict reaction-library model was not processed")

    write_reactions_table(parent, output, strict_model)
    with (output / "ec-aliases.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source", "source_id", "canonical_id", "ec", "status"],
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerows(alias_ledger)

    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    manifest["parent_library"] = {
        "path": str(parent),
        "manifest_sha256": sha256(parent / "manifest.json"),
    }
    manifest["ec_aliases"] = {
        **alias_stats,
        "source": str(alias_source),
        "source_sha256": sha256(alias_source),
        "policy": "annotations_only; no stoichiometry, bounds or direction changes",
    }
    manifest["sources"] = dict(manifest.get("sources", {}))
    manifest["sources"]["ec_aliases"] = {
        "path": str(alias_source),
        "sha256": sha256(alias_source),
    }
    manifest["model_sha256"] = model_hashes["universe.xml.gz"]
    manifest["full_union_sha256"] = model_hashes["universe_full.xml.gz"]
    manifest["bigg_model_sha256"] = model_hashes["universe_bigg.xml.gz"]
    manifest["bigg_full_model_sha256"] = model_hashes["universe_bigg_full.xml.gz"]
    recipe = dict(manifest.get("build_recipe", {}))
    recipe["builder_version"] = f"{recipe.get('builder_version', '1')}+ec_aliases"
    recipe["source_hashes"] = dict(recipe.get("source_hashes", {}))
    recipe["source_hashes"]["ec_alias_source"] = sha256(alias_source)
    recipe["source_versions"] = dict(recipe.get("source_versions", {}))
    recipe["source_versions"]["ec_alias_source"] = "local_snapshot"
    recipe["parameters"] = dict(recipe.get("parameters", {}))
    recipe["parameters"]["ec_alias_source"] = str(alias_source)
    manifest["build_recipe"] = recipe
    manifest["implementation_sha256"] = sha256(Path(__file__).resolve())
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(output), "ec_aliases": alias_stats, "model_hashes": model_hashes},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
