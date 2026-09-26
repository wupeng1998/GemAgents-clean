"""Deterministic union of public BiGG model directions and metadata."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash
from GemAgents.metabolic.library.normalization import metabolic_equation_key
from GemAgents.metabolic.library.source_io import load_source_model


def _merge_reaction_annotations(target, source) -> None:
    """Union public reaction annotations without discarding canonical values."""
    for key, raw in source.annotation.items():
        if raw in (None, "", [], {}):
            continue
        current = target.annotation.get(key)
        if isinstance(raw, dict):
            merged = dict(current) if isinstance(current, dict) else {}
            for nested_key, nested_value in raw.items():
                if nested_value not in (None, "", [], {}):
                    merged[nested_key] = nested_value
            target.annotation[key] = merged
            continue
        source_values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        current_values = current if isinstance(current, (list, tuple, set)) else [current]
        values = {
            str(value) for value in (*current_values, *source_values) if value not in (None, "")
        }
        if values:
            target.annotation[key] = sorted(values)


def metabolic_apply_bigg_direction_union(model, model_directory: Path) -> tuple[dict, list[dict]]:
    """Merge public BiGG model variants and union identical-equation directions."""
    from cobra import Metabolite, Reaction

    paths = sorted(
        [
            *model_directory.glob("*.xml"),
            *model_directory.glob("*.xml.gz"),
            *model_directory.glob("*.json"),
            *model_directory.glob("*.json.gz"),
        ]
    )
    if not paths:
        raise ToolError(f"No BiGG SBML models found in {model_directory}")
    direction_sources = defaultdict(lambda: {"forward": set(), "reverse": set()})
    converted_bounds = defaultdict(list)
    public_sources = defaultdict(dict)
    metabolite_metadata = defaultdict(lambda: defaultdict(set))
    equation_targets = defaultdict(list)
    for candidate in model.reactions:
        candidate_key, _ = metabolic_equation_key(
            {metabolite.id: value for metabolite, value in candidate.metabolites.items()}
        )
        if candidate_key:
            equation_targets[candidate_key].append(candidate)
    equation_conflicts = []
    added_missing = []
    added_variants = []
    variant_ids = {}
    source_hashes = {}
    source_records = []
    matched = 0
    for path in paths:
        source = load_source_model(path)
        source_name = path.name
        for suffix in (".json.gz", ".xml.gz", ".json", ".xml"):
            source_name = source_name.removesuffix(suffix)
        source_hash = metabolic_hash(path)
        source_hashes[path.name] = source_hash
        for metabolite in source.metabolites:
            try:
                charge = float(metabolite.charge)
            except (TypeError, ValueError):
                continue
            if metabolite.formula and math.isfinite(charge):
                if charge.is_integer():
                    charge = int(charge)
                metabolite_metadata[metabolite.id][(metabolite.formula, charge)].add(source_name)
        for reaction in source.reactions:
            identifier = reaction.id.lower()
            if identifier in {"growth", "biomass"} or "biomass" in identifier:
                continue
            source_key, source_pivot = metabolic_equation_key(
                {
                    metabolite.id: coefficient
                    for metabolite, coefficient in reaction.metabolites.items()
                }
            )
            record_status = "matched_same_id"
            target = (
                model.reactions.get_by_id(reaction.id) if reaction.id in model.reactions else None
            )
            if target is not None:
                target_key, _ = metabolic_equation_key(
                    {
                        metabolite.id: coefficient
                        for metabolite, coefficient in target.metabolites.items()
                    }
                )
                if source_key != target_key:
                    equation_conflicts.append({"model": source_name, "reaction": reaction.id})
                    equivalent = equation_targets.get(source_key, [])
                    if len(equivalent) == 1:
                        target = equivalent[0]
                        record_status = "equivalent_equation_alias"
                    else:
                        variant_key = (reaction.id, source_key)
                        variant_id = variant_ids.get(variant_key)
                        if variant_id is None:
                            digest = hashlib.sha256(repr(source_key).encode("utf-8")).hexdigest()[
                                :12
                            ]
                            variant_id = f"BIGGVAR_{reaction.id}_{digest}"
                            suffix = 2
                            base_variant_id = variant_id
                            while variant_id in model.reactions:
                                variant_id = f"{base_variant_id}_{suffix}"
                                suffix += 1
                            variant_ids[variant_key] = variant_id
                            target = None
                            record_status = "added_equation_variant"
                        else:
                            target = model.reactions.get_by_id(variant_id)
                            record_status = "matched_equation_variant"
            elif source_key and len(equation_targets.get(source_key, [])) == 1:
                target = equation_targets[source_key][0]
                record_status = "equivalent_equation_alias"
            if target is None:
                target_id = variant_ids.get((reaction.id, source_key), reaction.id)
                if record_status != "added_equation_variant":
                    record_status = "added_missing_reaction"
                stoichiometry = {}
                for metabolite, coefficient in reaction.metabolites.items():
                    if metabolite.id not in model.metabolites:
                        copied = Metabolite(
                            metabolite.id,
                            name=metabolite.name,
                            formula=metabolite.formula,
                            charge=metabolite.charge,
                            compartment=metabolite.compartment,
                        )
                        copied.annotation = dict(metabolite.annotation)
                        model.add_metabolites([copied])
                    stoichiometry[model.metabolites.get_by_id(metabolite.id)] = coefficient
                target = Reaction(target_id, name=reaction.name)
                # A universal-library reaction is optional.  A positive
                # source lower bound usually represents maintenance or a
                # model-specific task and must not become a forced flux in
                # every reconstruction.
                target.bounds = (
                    min(float(reaction.lower_bound), 0.0),
                    max(float(reaction.upper_bound), 0.0),
                )
                target.add_metabolites(stoichiometry)
                target.annotation = dict(reaction.annotation)
                target.annotation["bigg.reaction"] = reaction.id
                target.notes["public_bigg_source_model"] = source_name
                target.notes["public_bigg_source_reaction"] = reaction.id
                target.gene_reaction_rule = ""
                model.add_reactions([target])
                if source_key:
                    equation_targets[source_key].append(target)
                if target.id == reaction.id:
                    event = {
                        "event": "added_missing_reaction",
                        "model": source_name,
                        "source_sha256": source_hash,
                        "reaction": reaction.id,
                        "canonical_id": target.id,
                        "source_equation": reaction.reaction,
                        "source_bounds": list(reaction.bounds),
                    }
                    added_missing.append(event)
                else:
                    event = {
                        "event": "added_equation_variant",
                        "model": source_name,
                        "source_sha256": source_hash,
                        "reaction": reaction.id,
                        "canonical_id": target.id,
                        "source_equation": reaction.reaction,
                        "source_bounds": list(reaction.bounds),
                    }
                    added_variants.append(event)
            aliases = target.annotation.get("bigg.reaction", [])
            aliases = [aliases] if isinstance(aliases, str) else list(aliases)
            target.annotation["bigg.reaction"] = sorted(set(aliases + [reaction.id]))
            _merge_reaction_annotations(target, reaction)
            matched += 1
            _, target_pivot = metabolic_equation_key(
                {
                    metabolite.id: coefficient
                    for metabolite, coefficient in target.metabolites.items()
                }
            )
            # Identical normalized equations can differ by both scale and
            # sign.  Convert source flux bounds into the target equation's
            # coordinate system before combining directions.
            flux_scale = source_pivot / target_pivot
            converted = sorted(float(value) * flux_scale for value in reaction.bounds)
            converted_bounds[target.id].append((converted[0], converted[1]))
            if converted[1] > 1e-12:
                direction_sources[target.id]["forward"].add(source_name)
            if converted[0] < -1e-12:
                direction_sources[target.id]["reverse"].add(source_name)
            public_sources[target.id][source_name] = {
                "reaction": reaction.id,
                "sha256": source_hash,
                "flux_scale": flux_scale,
                "converted_bounds": converted,
            }
            source_records.append(
                {
                    "event": "public_bigg_source_record",
                    "status": record_status,
                    "model": source_name,
                    "source_sha256": source_hash,
                    "reaction": reaction.id,
                    "canonical_id": target.id,
                    "source_equation": reaction.reaction,
                    "canonical_equation": target.reaction,
                    "source_bounds": list(reaction.bounds),
                    "converted_bounds": converted,
                    "flux_scale": flux_scale,
                }
            )
    rows = []
    for reaction_id, directions in sorted(direction_sources.items()):
        reaction = model.reactions.get_by_id(reaction_id)
        before = tuple(float(value) for value in reaction.bounds)
        intervals = converted_bounds[reaction_id]
        lower = min([before[0], 0.0, *(item[0] for item in intervals)])
        upper = max([before[1], 0.0, *(item[1] for item in intervals)])
        reaction.bounds = (lower, upper)
        reaction.notes["public_bigg_sources"] = json.dumps(
            public_sources[reaction_id], sort_keys=True
        )
        if reaction.bounds != before:
            rows.append(
                {
                    "event": "bounds_union",
                    "reaction": reaction_id,
                    "before": list(before),
                    "after": list(reaction.bounds),
                    "forward_models": sorted(directions["forward"]),
                    "reverse_models": sorted(directions["reverse"]),
                }
            )
    enriched_metabolites = 0
    metadata_conflicts = 0
    for metabolite_id, candidates in metabolite_metadata.items():
        if metabolite_id not in model.metabolites:
            continue
        metabolite = model.metabolites.get_by_id(metabolite_id)
        if len(candidates) != 1:
            if not metabolite.formula or metabolite.charge is None:
                metadata_conflicts += 1
            continue
        (formula, charge), sources = next(iter(candidates.items()))
        try:
            current_charge_valid = math.isfinite(float(metabolite.charge))
        except (TypeError, ValueError):
            current_charge_valid = False
        if not metabolite.formula or not current_charge_valid:
            metabolite.formula = formula
            metabolite.charge = charge
            metabolite.notes["public_bigg_metadata_sources"] = json.dumps(sorted(sources))
            enriched_metabolites += 1
    return (
        {
            "models": len(paths),
            "matched_reaction_records": matched,
            "widened_reactions": len(rows),
            "equation_conflicts": len(equation_conflicts),
            "added_missing_reactions": len(added_missing),
            "added_equation_variants": len(added_variants),
            "enriched_metabolites": enriched_metabolites,
            "metabolite_metadata_conflicts": metadata_conflicts,
            "source_files": source_hashes,
            "policy": (
                "merge missing public reactions; preserve same-ID equation variants "
                "under provenance IDs; union directions for identical equations"
            ),
        },
        rows + source_records,
    )
