"""Deterministic public biomass catalog compilation.

The compiler owns catalog orchestration while the historical ``legacy`` module
only supplies compatibility callbacks.  No source is fetched implicitly; every
model and reference path comes from the declared specification.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from GemAgents.errors import ToolError
from GemAgents.metabolic.biomass.catalog import (
    add_reference_alias_rules,
    build_metabolite_index,
    build_reference_gpr_rules,
    build_support_reactions,
    collect_empirical_assembly_ids,
    load_reference_record,
    map_biomass_stoichiometry,
    merge_partial_support,
)

_FE_S_SUPPORT_IDS = frozenset(
    {
        "S2FE2SR", "S2FE2SS", "S4FE4SR", "I4FE4SR", "S2FE2ST", "I2FE2SS",
        "I2FE2ST", "I2FE2SS2", "S2FE2SS2", "BTS5", "LIPOS", "I2FE2SR",
        "DBTS", "UPP3MT", "SHCHF", "SHCHD2", "DB4PS", "RBFSa", "RBFSb",
        "RBFK", "MCTP1App", "CPPPGO2", "5DOAN", "MOADSUx", "SCYSDS",
        "ICYSDS", "FESR", "I4FE4ST", "S4FE4ST", "LIPOCT", "LIPAMPL",
        "FESD1s", "FESD2s", "TYRL", "THZPSN3", "OCTNLL",
    }
)


def compile_biomass_library(
    workspace: Path,
    spec_path: Path,
    out: Path,
    reaction_library: Path,
    *,
    native_universe_fn: Callable[[dict], tuple[Any, Path]],
    compartment_fn: Callable[[str], str],
    load_source_model_fn: Callable[[Path], Any],
    reference_proteins_fn: Callable[[Path], list[dict]],
    fasta_fn: Callable[[Path, str], list[tuple[str, str]]],
    hash_fn: Callable[[Path], str],
    write_json_fn: Callable[[Path, object], None],
    sequence_sketch_fn: Callable[..., list[int]],
    restricted_path_fn: Callable[[Path], bool],
    is_assembly_fn: Callable[[object, set[str]], bool],
    is_pool_fn: Callable[[object, set[str]], bool],
) -> dict:
    """Compile declared public biomass templates into a normalized catalog."""
    if out.exists() and any(out.iterdir()):
        raise ToolError("Biomass library output must be a new or empty directory")
    out.mkdir(parents=True, exist_ok=True)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sources = spec if isinstance(spec, list) else spec.get("sources", [])
    if not isinstance(sources, list) or not sources:
        raise ToolError("Biomass spec requires a nonempty sources list")
    universe = native_universe_fn({"reaction_library": str(reaction_library)})[0]
    met_index = build_metabolite_index(universe, compartment_fn)
    templates, references, failures = [], {}, []
    for source in sources:
        sid = str(source.get("id", "")).strip()
        if not sid or not source.get("model"):
            failures.append({"source": sid, "reason": "missing_id_or_model"})
            continue
        model_path = (workspace / source["model"]).resolve()
        try:
            model = load_source_model_fn(model_path)
            explicit_metabolite_map = {
                str(k): str(v) for k, v in source.get("metabolite_id_map", {}).items()
            }
            requested_biomass = source.get("biomass_reaction")
            if requested_biomass:
                if requested_biomass not in model.reactions:
                    raise ToolError("declared biomass_reaction is absent from source model")
                biomass = model.reactions.get_by_id(requested_biomass)
            else:
                objectives = [r for r in model.reactions if abs(r.objective_coefficient) > 1e-12]
                if len(objectives) != 1:
                    raise ToolError("source model must have exactly one objective reaction")
                biomass = objectives[0]
            support_metabolite_ids = {
                metabolite.id
                for reaction in model.reactions
                if reaction.id in _FE_S_SUPPORT_IDS
                for metabolite in reaction.metabolites
            }
            # Source biomass equations may consume explicit BIOMASS_* pool
            # products that are absent from the canonical universe.  Include
            # those declared assembly products in the source-to-canonical map;
            # build_support_reactions adds the same reactions with provenance.
            assembly_ids = collect_empirical_assembly_ids(
                model,
                biomass,
                is_assembly=is_assembly_fn,
                is_pool=is_pool_fn,
            )
            support_metabolite_ids.update(
                metabolite.id
                for reaction_id in assembly_ids
                for metabolite, coefficient in model.reactions.get_by_id(
                    reaction_id
                ).metabolites.items()
                if coefficient > 0
            )
            mapped_stoich, mapped_source_ids, unmapped = map_biomass_stoichiometry(
                biomass,
                universe,
                met_index,
                explicit_metabolite_map,
                support_metabolite_ids,
                compartment_fn,
            )
            _reference, proteins, reference_record = load_reference_record(
                source,
                workspace,
                reference_proteins=reference_proteins_fn,
                fasta_reader=fasta_fn,
                hash_path=hash_fn,
            )
            references[reference_record["id"]] = reference_record
            rules = build_reference_gpr_rules(model, biomass, universe)
            rules, support_aliases = add_reference_alias_rules(model, universe, rules)
            medium_support_reactions = []
            if "FE3t" in universe.reactions:
                medium_support_reactions.append(
                    {
                        "id": "FE3t",
                        "role": "medium_support",
                        "reason": "rich_medium_fe3_import_without_sequence_assertion",
                    }
                )
            support_reactions, _assembly_ids, support_rules = build_support_reactions(
                model,
                biomass,
                model_path,
                source,
                hash_path_fn=hash_fn,
                is_assembly_fn=is_assembly_fn,
                is_pool_fn=is_pool_fn,
            )
            rules.extend(
                row
                for row in support_rules
                if not any(existing["reaction_id"] == row["reaction_id"] for existing in rules)
            )
            support_reactions, partial_report = merge_partial_support(
                source.get("partial_support", {}),
                workspace,
                support_reactions,
                restricted_path_fn=restricted_path_fn,
            )
            genome_path = source.get("reference_fna")
            genome_sketch = []
            protein_sketch = []
            if genome_path:
                genome_path = (workspace / genome_path).resolve()
                genome_sketch = sequence_sketch_fn(fasta_fn(genome_path, "fna"), "dna")
            if proteins:
                protein_sketch = sequence_sketch_fn(
                    [(p["gene_id"], p["sequence"]) for p in proteins],
                    "protein",
                    k=7,
                )
            templates.append(
                {
                    "id": sid,
                    "organism": source.get("organism", sid),
                    "kingdom": source.get("kingdom", "bacteria"),
                    "gram": source.get("gram", "unspecified"),
                    "source_url": source.get("source_url", ""),
                    "license": source.get("license", ""),
                    "model": str(model_path),
                    "model_sha256": hash_fn(model_path),
                    "reference_id": reference_record["id"],
                    "biomass_source_id": biomass.id,
                    "biomass_reaction": {
                        "id": biomass.id,
                        "name": biomass.name,
                        "stoichiometry": mapped_stoich,
                        "source_id": biomass.id,
                        "source_stoichiometry": {
                            metabolite.id: {
                                "coefficient": float(coefficient),
                                "compartment": metabolite.compartment,
                                "formula": metabolite.formula,
                                "charge": metabolite.charge,
                                "annotation": metabolite.annotation,
                            }
                            for metabolite, coefficient in biomass.metabolites.items()
                        },
                        "source_to_canonical": mapped_source_ids,
                    },
                    "gpr_templates": rules,
                    "support_reactions": support_reactions,
                    "support_aliases": support_aliases,
                    "medium_support_reactions": medium_support_reactions,
                    "partial_support": partial_report,
                    "sketch": genome_sketch or protein_sketch,
                    "sketch_alphabet": "dna" if genome_sketch else "protein",
                    "genome_sketch": genome_sketch,
                    "protein_sketch": protein_sketch,
                    "usable": not unmapped and bool(mapped_stoich),
                    "unmapped_biomass_metabolites": sorted(set(unmapped)),
                }
            )
        except (OSError, ValueError, KeyError, ToolError) as error:
            failures.append({"source": sid, "reason": type(error).__name__, "error": str(error)})
    result = {
        "schema_version": 1,
        "status": "complete" if templates else "failed",
        "reaction_library": str(reaction_library),
        "reaction_library_manifest_sha256": hash_fn(reaction_library / "manifest.json"),
        "templates": templates,
        "references": references,
        "failures": failures,
        "sources_count": len(sources),
        "usable_templates": sum(t["usable"] for t in templates),
        "scope": "public biomass templates; sequence sketch ranks similarity and is not ANI",
    }
    write_json_fn(out / "catalog.json", result)
    write_json_fn(out / "manifest.json", {k: v for k, v in result.items() if k != "references"})
    write_json_fn(out / "references.json", references)
    return result


__all__ = ["compile_biomass_library"]
