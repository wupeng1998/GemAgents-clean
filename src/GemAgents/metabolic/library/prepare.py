"""Auditable construction of the public reaction library."""

from __future__ import annotations

import csv
import json
import math
import shutil
import time
from collections import defaultdict
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash, metabolic_json
from GemAgents.metabolic.library.direction_union import metabolic_apply_bigg_direction_union
from GemAgents.metabolic.library.ec_aliases import apply_ec_aliases
from GemAgents.metabolic.library.merge import metabolic_merge_libraries
from GemAgents.metabolic.library.quality import metabolic_quality_control_library
from GemAgents.metabolic.library.universe import metabolic_universe
from GemAgents.metabolic.network import metabolic_download


def metabolic_prepare_library(workspace: Path, out: Path, config: dict | None = None) -> dict:
    """Build one auditable BiGG/ModelSEED library with strict and operational views."""
    from importlib.metadata import PackageNotFoundError, version
    from importlib.resources import files

    from cobra.io import read_sbml_model, write_sbml_model

    config = config or {}

    def installed_version(package: str) -> str | None:
        try:
            return version(package)
        except PackageNotFoundError:
            return None

    if (out / "manifest.json").exists():
        raise ToolError("Library already exists; use a new directory for a new build")
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    sources = out / "sources"
    sources.mkdir(exist_ok=True)
    references = {}
    tables = {}
    cached_sources = (
        Path(config["modelseed_source_directory"])
        if config.get("modelseed_source_directory")
        else None
    )
    for name in ("compounds", "reactions"):
        url = f"https://raw.githubusercontent.com/ModelSEED/ModelSEEDDatabase/master/Biochemistry/{name}.tsv"
        path = sources / f"modelseed_{name}.tsv"
        cached = cached_sources / path.name if cached_sources else None
        if cached is not None:
            if not cached.is_file():
                raise ToolError(f"Cached ModelSEED source is missing: {cached}")
            shutil.copyfile(cached, path)
            references[name] = {
                "source": str(cached.resolve()),
                "upstream_url": url,
                "sha256": metabolic_hash(path),
                "bytes": path.stat().st_size,
            }
        else:
            metabolic_download(url, path)
            references[name] = {
                "url": url,
                "sha256": metabolic_hash(path),
                "bytes": path.stat().st_size,
            }
        with path.open(encoding="utf-8") as handle:
            tables[name] = {r["id"]: r for r in csv.DictReader(handle, delimiter="\t")}
    print("[reaction library] loading public reconstruction models", flush=True)
    bigg, bigg_path = metabolic_universe({**config, "engine": "carveme"})
    seed_path = (
        Path(config["modelseed_universe"])
        if config.get("modelseed_universe")
        else Path(str(files("reconstructor.resources") / "universal.sbml.gz"))
    )
    if not seed_path.is_file():
        raise ToolError(f"ModelSEED universal model is missing: {seed_path}")
    seed = read_sbml_model(str(seed_path))
    seed.solver = "glpk"
    print("[reaction library] checking compounds and equations", flush=True)
    merged, catalog, met_rows, stats = metabolic_merge_libraries(
        bigg, seed, tables["compounds"], tables["reactions"]
    )
    for metabolite in merged.metabolites:
        if isinstance(metabolite.charge, float) and math.isfinite(metabolite.charge):
            if metabolite.charge.is_integer():
                metabolite.charge = int(metabolite.charge)
    print("[reaction library] auditing directions and conservation", flush=True)
    direction_union = None
    direction_rows = []
    if config.get("bigg_models_directory"):
        direction_union, direction_rows = metabolic_apply_bigg_direction_union(
            merged, Path(config["bigg_models_directory"])
        )
        metabolic_json(
            out / "bigg_direction_union.json",
            {
                **direction_union,
                "reactions": [
                    row for row in direction_rows if row.get("event") != "public_bigg_source_record"
                ],
                "source_records_location": "reaction_catalog.jsonl",
            },
        )
        for event in direction_rows:
            if event.get("event") != "public_bigg_source_record":
                continue
            canonical = merged.reactions.get_by_id(event["canonical_id"])
            catalog.append(
                {
                    "source": "public_bigg_model_collection",
                    "source_id": event["reaction"],
                    "canonical_id": event["canonical_id"],
                    "source_model": event["model"],
                    "source_sha256": event["source_sha256"],
                    "source_equation": event["source_equation"],
                    "canonical_equation": canonical.reaction,
                    "source_bounds": event["source_bounds"],
                    "converted_bounds": event["converted_bounds"],
                    "flux_scale": event["flux_scale"],
                    "canonical_bounds": list(canonical.bounds),
                    "status": event["status"],
                    "active": True,
                }
            )
        for row in catalog:
            if row["canonical_id"] in merged.reactions:
                row["canonical_bounds"] = list(
                    merged.reactions.get_by_id(row["canonical_id"]).bounds
                )
        stats["public_bigg_source_records"] = sum(
            row.get("source") == "public_bigg_model_collection" for row in catalog
        )
        stats["public_bigg_added_reactions"] = direction_union["added_missing_reactions"]
        stats["public_bigg_equation_variants"] = direction_union["added_equation_variants"]
    full_union = merged.copy()
    ec_alias_ledger: list[dict[str, str]] = []
    ec_alias_stats = {"rows": 0, "matched": 0, "unmatched": 0}
    alias_value = config.get("ec_alias_source") or config.get("ec_alias_sources")
    alias_paths = (
        [Path(alias_value)]
        if isinstance(alias_value, str)
        else [Path(value) for value in (alias_value or [])]
    )
    if alias_value:
        ec_alias_ledger, ec_alias_stats = apply_ec_aliases(merged, alias_paths)
        full_union = merged.copy()
    high_precision, reaction_quality, quality_report = metabolic_quality_control_library(merged)
    quality_by_id = reaction_quality
    build_recipe = {
        "builder_version": "1",
        "source_versions": {
            "modelseed_compounds": str(config.get("modelseed_version") or "unresolved"),
            "modelseed_reactions": str(config.get("modelseed_version") or "unresolved"),
            "bigg": str(installed_version("carveme") or "configured_snapshot"),
            "reconstructor": str(installed_version("reconstructor") or "unresolved"),
            **{f"ec_alias_{index}": "local_snapshot" for index, _ in enumerate(alias_paths)},
        },
        "source_hashes": {
            "modelseed_compounds": references["compounds"]["sha256"],
            "modelseed_reactions": references["reactions"]["sha256"],
            "bigg": metabolic_hash(bigg_path),
            "reconstructor": metabolic_hash(seed_path),
            **{
                f"ec_alias_{index}": metabolic_hash(path)
                for index, path in enumerate(alias_paths)
            },
        },
        "parameters": {
            "kingdom": config.get("kingdom", "bacteria"),
            "gram": config.get("gram", "unspecified"),
            "direction_union": bool(config.get("bigg_models_directory")),
            "ec_alias_sources": [str(path.resolve()) for path in alias_paths]
            if alias_paths else [],
        },
    }
    for row in catalog:
        qc = quality_by_id.get(row["canonical_id"])
        row["qc_status"] = qc["status"] if qc else "not_in_active_model"
        row["qc_direction"] = qc["direction"] if qc else "unknown"
        row["chemistry_class"] = qc["chemistry_class"] if qc else "unknown_chemistry"
        row["isolation_scope"] = qc["isolation_scope"] if qc else "catalog_only"
        row["source_version"] = "see_build_recipe"
        row["active_after_qc"] = bool(qc and qc["active"])
    print("[reaction library] writing library and provenance", flush=True)
    if ec_alias_ledger:
        with (out / "ec-aliases.tsv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["source", "source_id", "canonical_id", "ec", "status"],
                delimiter="\t",
            )
            writer.writeheader()
            writer.writerows(ec_alias_ledger)
    with (out / "reaction_catalog.jsonl").open("w", encoding="utf-8") as handle:
        for row in catalog:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    sources_by_reaction = defaultdict(set)
    for row in catalog:
        sources_by_reaction[row["canonical_id"]].add(row["source"])
    with (out / "reaction_evidence.jsonl").open("w", encoding="utf-8") as handle:
        for reaction_id, qc in sorted(quality_by_id.items()):
            sources_for_reaction = sorted(sources_by_reaction.get(reaction_id, {"unresolved"}))
            record = {
                "reaction_id": reaction_id,
                "chemistry_class": qc["chemistry_class"],
                "source": ",".join(sources_for_reaction),
                "source_version": "see_recipe",
                "recipe": build_recipe,
            }
            if sources_for_reaction == ["unresolved"]:
                record["unresolved_reason"] = "no source catalog record"
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    for filename, rows in (("compound_mapping.tsv", met_rows), ("reaction_mapping.tsv", catalog)):
        keys = list(dict.fromkeys(key for row in rows for key in row))
        with (out / filename).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys, delimiter="\t")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                        for k, v in row.items()
                    }
                )
    with (out / "reactions.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(
            ["id", "bigg_ids", "modelseed_ids", "equation", "lower_bound", "upper_bound", "ec"]
        )
        for r in high_precision.reactions:
            writer.writerow(
                [
                    r.id,
                    json.dumps(r.annotation.get("bigg.reaction", [])),
                    json.dumps(r.annotation.get("seed.reaction", [])),
                    r.reaction,
                    r.lower_bound,
                    r.upper_bound,
                    json.dumps(r.annotation.get("ec-code", [])),
                ]
            )
    bigg_model = high_precision.copy()
    bigg_ids = {
        reaction.id for reaction in full_union.reactions if reaction.annotation.get("bigg.reaction")
    }
    bigg_model.remove_reactions([r for r in bigg_model.reactions if r.id not in bigg_ids])
    bigg_full_model = full_union.copy()
    bigg_full_model.remove_reactions([r for r in bigg_full_model.reactions if r.id not in bigg_ids])
    # CarveMe's MILP runs on the quality-controlled BiGG subset.  The complete
    # pre-QC union remains available for provenance and re-audit.
    write_sbml_model(full_union, str(out / "universe_full.xml.gz"))
    write_sbml_model(bigg_model, str(out / "universe_bigg.xml.gz"))
    write_sbml_model(bigg_full_model, str(out / "universe_bigg_full.xml.gz"))
    write_sbml_model(high_precision, str(out / "universe.xml.gz"))
    metabolic_json(out / "reaction_quality.json", quality_report)
    with (out / "reaction_quality.tsv").open("w", encoding="utf-8", newline="") as handle:
        fields = [
            "reaction_id",
            "direction",
            "lower_bound",
            "upper_bound",
            "boundary_or_pseudoreaction",
            "balance_residual",
            "chemistry_class",
            "isolation_scope",
            "status",
            "active",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for row in quality_by_id.values():
            writer.writerow(
                {
                    **row,
                    "balance_residual": json.dumps(row["balance_residual"], sort_keys=True),
                }
            )
    manifest = {
        "status": "complete",
        "schema_version": 1,
        "kingdom": config.get("kingdom", "bacteria"),
        "applicable_kingdoms": (
            ["bacteria", "archaea"]
            if config.get("kingdom", "bacteria") in {"bacteria", "archaea"}
            else [config.get("kingdom", "bacteria")]
        ),
        "gram": config.get("gram", "unspecified"),
        "biomass_policy": (
            "Unified public biomass interfaces are retained in catalog; native models select "
            "a sequence-ranked template"
        ),
        "merge_policy": (
            "Unique aliases and chemistry, no InChIKey conflict; compartmental equations"
        ),
        "bounds_policy": "Keep representative bounds, record all conflicting source bounds",
        "chemistry_policy": (
            "Public SEED reference metadata; only balanced new internal reactions active"
        ),
        "counts": {
            "source_bigg": len(bigg.reactions),
            "source_modelseed": len(seed.reactions),
            "catalog": len(catalog),
            "active_reactions": len(high_precision.reactions),
            "active_metabolites": len(high_precision.metabolites),
            "full_union_reactions": len(full_union.reactions),
            "full_union_metabolites": len(full_union.metabolites),
            "mapping": stats,
            "mapped_compounds": sum(
                r["status"] == "verified_alias_and_chemistry" for r in met_rows
            ),
        },
        "sources": {
            **references,
            "bigg": {
                "provider": "configured_universe" if config.get("universe") else "carveme",
                "version": None if config.get("universe") else installed_version("carveme"),
                "path": str(bigg_path),
                "sha256": metabolic_hash(bigg_path),
            },
            "reconstructor": {
                "version": installed_version("reconstructor"),
                "path": str(seed_path),
                "sha256": metabolic_hash(seed_path),
            },
            "ec_aliases": [
                {"path": str(path.resolve()), "sha256": metabolic_hash(path)}
                for path in alias_paths
            ],
        },
        "model_sha256": metabolic_hash(out / "universe.xml.gz"),
        "full_union_sha256": metabolic_hash(out / "universe_full.xml.gz"),
        "bigg_model_sha256": metabolic_hash(out / "universe_bigg.xml.gz"),
        "bigg_full_model_sha256": metabolic_hash(out / "universe_bigg_full.xml.gz"),
        "bigg_active_reactions": len(bigg_model.reactions),
        "bigg_full_reactions": len(bigg_full_model.reactions),
        "quality_control": quality_report,
        "canonical_chemistry_corrections": quality_report.get(
            "canonical_chemistry_corrections", []
        ),
        "energy_direction_guards": [
            row["reaction_id"] for row in quality_report.get("energy_direction_guards", [])
        ],
        "build_recipe": build_recipe,
        "direction_union": direction_union,
        "ec_aliases": ec_alias_stats,
        "implementation_sha256": metabolic_hash(Path(__file__)),
        "elapsed_seconds": time.perf_counter() - started,
        "excluded_projects_used": False,
        "scope": "Public reaction library; biological accuracy and cycle freedom are unverified",
    }
    metabolic_json(out / "manifest.json", manifest)
    return manifest
