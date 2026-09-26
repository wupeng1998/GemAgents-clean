"""Conservative union of public reaction libraries with source ledger preservation.

This leaf keeps the legacy merge semantics while isolating library construction
from the compatibility facade.
"""

from __future__ import annotations

import json
import math
import re

from GemAgents.metabolic.library.normalization import (
    metabolic_aliases,
    metabolic_compartment,
    metabolic_equation_key,
    metabolic_normalize_ec_values,
)


def metabolic_merge_libraries(bigg, seed, compounds: dict, reactions: dict):
    """Create a conservative BiGG-based union plus a lossless source reaction ledger.

    Public ModelSEED compound metadata defines the standardized SEED representation.
    Source metadata and equations remain in the ledger. New internal SEED reactions
    enter the active bag only if the standardized equation balances mass and charge.
    """
    from collections import Counter, defaultdict

    from cobra import Reaction

    merged = bigg.copy()
    merged.id = "bigg_modelseed_universe"
    comp_names = {metabolic_compartment(m.compartment): m.compartment for m in bigg.metabolites}
    bigg_index = defaultdict(set)
    for met in bigg.metabolites:
        code = metabolic_compartment(met.compartment)
        base = met.id.removesuffix("_" + code)
        bigg_index[(base, code)].add(met.id)
        met_out = merged.metabolites.get_by_id(met.id)
        met_out.annotation["bigg.metabolite"] = base
        met_out.annotation.pop("seed.compound", None)

    normalized, candidate_ids, met_rows = {}, {}, {}
    for met in seed.metabolites:
        code = metabolic_compartment(met.compartment)
        base = met.id.removesuffix("_" + code)
        record = compounds.get(base, {})
        current = met.copy()
        current.compartment = comp_names.get(code, code)
        verified_metadata = False
        if record and record.get("is_obsolete", "0") == "0":
            try:
                formula, charge = record["formula"], float(record["charge"])
                if formula and formula != "null" and math.isfinite(charge):
                    # libSBML/COBRApy 0.32 serializes integral charges stored
                    # as floats as zero; preserve them as Python integers.
                    normalized_charge = int(charge) if charge.is_integer() else charge
                    current.formula, current.charge = formula, normalized_charge
                    verified_metadata = True
            except (KeyError, ValueError):
                pass
        aliases = metabolic_aliases(record.get("aliases", ""))
        candidates, structural_conflicts = set(), set()
        if verified_metadata and current.elements and "R" not in current.elements:
            for alias in aliases:
                for target in bigg_index.get((alias, code), ()):
                    other = bigg.metabolites.get_by_id(target)
                    if current.elements == other.elements and current.charge == other.charge:
                        seed_key = record.get("inchikey", "")
                        bigg_keys = other.annotation.get("inchikey", [])
                        bigg_keys = [bigg_keys] if isinstance(bigg_keys, str) else bigg_keys
                        pattern = r"[A-Z]{14}-[A-Z]{10}-[A-Z]"
                        bigg_keys = [key for key in bigg_keys if re.fullmatch(pattern, key)]
                        if (
                            re.fullmatch(pattern, seed_key)
                            and bigg_keys
                            and seed_key not in bigg_keys
                        ):
                            structural_conflicts.add(target)
                        else:
                            candidates.add(target)
        normalized[met.id] = current
        candidate_ids[met.id] = candidates
        met_rows[met.id] = {
            "modelseed_id": met.id,
            "canonical_id": met.id,
            "source_formula": met.formula,
            "source_charge": met.charge,
            "standard_formula": current.formula,
            "standard_charge": current.charge,
            "metadata_source": "public_ModelSEED" if verified_metadata else "unverified_source",
            "protonation_convention": "unresolved",
            "metadata_changed": (current.formula, current.charge) != (met.formula, met.charge),
            "compartment": code,
            "bigg_candidates": sorted(candidates),
            "structural_conflict_candidates": sorted(structural_conflicts),
            "status": "structure_conflict_kept_separate" if structural_conflicts else "unmapped",
        }
    met_map = {}
    for source, current in normalized.items():
        candidates = candidate_ids[source]
        target = next(iter(candidates)) if len(candidates) == 1 else None
        # A BiGG target remains canonical even when several ModelSEED source
        # IDs point to it.  Those source IDs are aliases of one chemical, not
        # evidence that the BiGG metabolite is ambiguous.  Only a source with
        # multiple possible BiGG targets is kept separate.
        if target:
            canonical = merged.metabolites.get_by_id(target)
            met_rows[source].update(canonical_id=target, status="verified_alias_and_chemistry")
        else:
            canonical = current
            if canonical.id in merged.metabolites:
                canonical.id = "MS_" + canonical.id
            met_rows[source]["canonical_id"] = canonical.id
            if candidates:
                met_rows[source]["status"] = "ambiguous_kept_separate"
        base = source.removesuffix("_" + met_rows[source]["compartment"])
        if re.fullmatch(r"cpd\d+", base):
            values = canonical.annotation.get("seed.compound", [])
            values = [values] if isinstance(values, str) else list(values)
            canonical.annotation["seed.compound"] = sorted(set(values + [base]))
        met_map[source] = canonical

    catalog, signatures, additions = [], {}, []
    source_ids = defaultdict(lambda: {"bigg": [], "modelseed": []})
    for reaction in sorted(merged.reactions, key=lambda r: r.id):
        if reaction.id != "Growth":
            reaction.annotation["bigg.reaction"] = reaction.id
        reaction.annotation.pop("seed.reaction", None)
        source_ids[reaction.id]["bigg"].append(reaction.id)
        key, pivot = metabolic_equation_key({m.id: v for m, v in reaction.metabolites.items()})
        signatures.setdefault(key, (reaction, pivot))
        catalog.append(
            {
                "source": "carveme_bigg",
                "source_id": reaction.id,
                "canonical_id": reaction.id,
                "source_equation": reaction.reaction,
                "canonical_equation": reaction.reaction,
                "source_bounds": list(reaction.bounds),
                "canonical_bounds": list(reaction.bounds),
                "status": "base_preserved",
                "active": True,
            }
        )
    for original in sorted(seed.reactions, key=lambda r: r.id):
        base = original.id.rsplit("_", 1)[0]
        reference = reactions.get(base, {})
        reaction = Reaction(original.id, name=original.name)
        if reaction.id in merged.reactions:
            reaction.id = "MS_" + reaction.id
        reaction.bounds = original.bounds
        stoich = defaultdict(float)
        for met, coefficient in original.metabolites.items():
            stoich[met_map[met.id]] += coefficient
        reaction.add_metabolites({m: v for m, v in stoich.items() if v})
        reaction.annotation = dict(original.annotation)
        if re.fullmatch(r"rxn\d+", base):
            reaction.annotation["seed.reaction"] = base
        ecs = metabolic_normalize_ec_values(reference.get("ec_numbers", ""))
        if ecs:
            reaction.annotation["ec-code"] = sorted(set(ecs))
        key, pivot = metabolic_equation_key({m.id: v for m, v in reaction.metabolites.items()})
        row = {
            "source": "reconstructor_modelseed",
            "source_id": original.id,
            "canonical_id": reaction.id,
            "source_equation": original.reaction,
            "canonical_equation": reaction.reaction,
            "source_bounds": list(original.bounds),
            "canonical_bounds": list(reaction.bounds),
            "modelseed_reference_equation": reference.get("equation", ""),
            "bigg_alias_candidates": metabolic_aliases(reference.get("aliases", "")),
            "active": False,
        }
        if key and key in signatures:
            canonical, other_pivot = signatures[key]
            scale = pivot / other_pivot
            converted = sorted(value * scale for value in original.bounds)
            row.update(
                canonical_id=canonical.id,
                canonical_equation=canonical.reaction,
                canonical_bounds=list(canonical.bounds),
                flux_scale=scale,
                status="equivalent_equation",
                active=True,
                bounds_disagree=not all(
                    math.isclose(a, b, abs_tol=1e-8)
                    for a, b in zip(converted, canonical.bounds, strict=True)
                ),
            )
            # Keep the selected representative's bounds: deduplication never widens fluxes.
            for namespace in ("seed.reaction", "ec-code"):
                old, new = (
                    canonical.annotation.get(namespace, []),
                    reaction.annotation.get(namespace, []),
                )
                old = [old] if isinstance(old, str) else list(old)
                new = [new] if isinstance(new, str) else list(new)
                if old or new:
                    canonical.annotation[namespace] = sorted(set(old + new))
            source_ids[canonical.id]["modelseed"].append(original.id)
        else:
            unknown = any(
                met_rows[m.id]["metadata_source"] != "public_ModelSEED"
                for m in original.metabolites
            ) or any(not m.elements or "R" in m.elements for m in reaction.metabolites)
            try:
                residual = reaction.check_mass_balance() if not unknown else {}
                residual = {k: v for k, v in residual.items() if abs(v) > 1e-8}
            except (TypeError, ValueError):
                unknown, residual = True, {}
            is_exchange = (
                original.id.startswith("EX_")
                and len(reaction.metabolites) == 1
                and all(metabolic_compartment(m.compartment) == "e" for m in reaction.metabolites)
            )
            if "biomass" in original.id.lower():
                status = "alternate_biomass_catalog_only"
            elif not key or unknown:
                status = "unverified_chemistry_catalog_only"
            elif original.boundary and not is_exchange:
                status = "nonexchange_boundary_catalog_only"
            elif residual and not is_exchange:
                status = "imbalanced_catalog_only"
            else:
                status = "added_exchange" if is_exchange else "added_balanced_reaction"
                row["active"] = True
                if is_exchange:
                    reaction.annotation["sbo"] = "SBO:0000627"
                additions.append(reaction)
                signatures[key] = (reaction, pivot)
                source_ids[reaction.id]["modelseed"].append(original.id)
            row.update(status=status, balance_residual=residual)
        catalog.append(row)
    merged.add_reactions(additions)
    for reaction in merged.reactions:
        reaction.notes["library_sources"] = json.dumps(source_ids[reaction.id], sort_keys=True)
    stats = dict(Counter(row["status"] for row in catalog))
    stats["bounds_disagreements"] = sum(row.get("bounds_disagree", False) for row in catalog)
    return merged, catalog, list(met_rows.values()), stats
