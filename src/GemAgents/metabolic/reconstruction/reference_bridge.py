"""Public-reference reaction bridge for native reconstruction."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from GemAgents.metabolic.library.normalization import metabolic_equation_key
from GemAgents.metabolic.library.quality import (
    apply_canonical_chemistry,
    guard_energy_hydrolysis_direction,
)
from GemAgents.metabolic.reconstruction.reference_support import compile_gpr


def _non_proton_stoichiometry(stoichiometry: dict[str, float]) -> dict[str, float]:
    """Project an equation onto non-proton species for protonation audits."""
    return {
        identifier: coefficient
        for identifier, coefficient in stoichiometry.items()
        if identifier.rsplit("_", 1)[0].casefold() not in {"h", "h2o"}
        and abs(coefficient) > 1e-12
    }


def resolve_reference_support_path(catalog_path: Path, config: dict, template: dict) -> Path:
    """Resolve a configured public reference path relative to the catalog."""
    reference_value = config.get("reference_support_path") or template.get("model")
    path = (
        Path(str(reference_value)).expanduser()
        if reference_value
        else catalog_path.parent / "__no_public_reference__"
    )
    if not path.is_absolute():
        path = (catalog_path.parent / path).resolve()
    return path


def add_reference_iml1515_support(
    universal,
    source_path: Path,
    reference_genes: dict,
    evidence: list[dict],
    proteins: dict,
    config: dict,
    reference_prefix: str = "REF_iML1515_",
    source_biomass_id: str = "BIOMASS_Ec_iML1515_core_75p37M",
) -> tuple[set[str], dict[str, dict], dict[str, dict], dict[str, object]]:
    """Import reference candidates or an explicitly requested reference scaffold.

    Candidate mode filters unverified and unbalanced internal reactions.
    Scaffold mode carries reference chemistry forward for the final QC audit;
    its reactions must remain labelled as reference evidence.
    """
    if not source_path.is_file() or not config.get("reference_support", True):
        return set(), {}, {}, {}
    from cobra import Reaction
    from cobra.io import model_from_dict, read_sbml_model

    try:
        source_name = source_path.name.lower()
        if source_name.endswith((".xml", ".xml.gz", ".sbml", ".sbml.gz")):
            source = read_sbml_model(str(source_path))
        elif source_name.endswith(".json.gz"):
            with gzip.open(source_path, "rt", encoding="utf-8") as handle:
                source = model_from_dict(json.load(handle))
        else:
            with source_path.open("r", encoding="utf-8") as handle:
                source = model_from_dict(json.load(handle))
    except (OSError, ValueError, TypeError, KeyError):
        return set(), {}, {}, {}

    # Normalize the source model before mapping shared IDs.  The operational
    # library owns canonical formulas and charges; importing a reference model
    # must never overwrite those definitions with legacy metadata.  Shared
    # source metadata is normalized before it can influence the scaffold, while
    # the source itself remains preserved as evidence.
    source_chemistry_corrections = apply_canonical_chemistry(source)
    source_direction_guards = []
    for source_reaction in source.reactions:
        if guard_energy_hydrolysis_direction(source_reaction):
            source_direction_guards.append(source_reaction.id)
    if source_chemistry_corrections:
        universal.notes["reference_source_chemistry_corrections"] = json.dumps(
            source_chemistry_corrections, sort_keys=True
        )
    if source_direction_guards:
        universal.notes["reference_source_direction_guards"] = json.dumps(
            sorted(source_direction_guards)
        )

    met_map = {}
    scaffold = bool(config.get("reference_scaffold"))
    pending_reactions = []
    pending_ids = set()
    compartments = {
        met.compartment: universal.metabolites.get_by_id(met.id).compartment
        for met in source.metabolites
        if met.id in universal.metabolites
    }
    for source_met in source.metabolites:
        if source_met.id in universal.metabolites:
            target = universal.metabolites.get_by_id(source_met.id)
            if target.elements != source_met.elements or target.charge != source_met.charge:
                # Keep the canonical target definition and record the source
                # disagreement for provenance.  Applying the source values to
                # the shared object would reintroduce static imbalance into
                # every dependent reaction.
                target.notes["reference_chemistry_conflict"] = json.dumps(
                    {
                        "source_formula": source_met.formula,
                        "source_charge": source_met.charge,
                        "native_formula": target.formula,
                        "native_charge": target.charge,
                    },
                    sort_keys=True,
                )
        else:
            target = source_met.copy()
            target.compartment = compartments.get(source_met.compartment, source_met.compartment)
            target.annotation["reference_source_metabolite"] = source_met.id
            universal.add_metabolites([target])
        met_map[source_met.id] = target

    exchange_equations = {
        tuple(sorted((met.id, coefficient) for met, coefficient in reaction.metabolites.items())):
        reaction.id
        for reaction in (
            universal.boundary if scaffold else universal.exchanges
        )
    }
    reference_exchanges = {}
    boundary_source_ids = (
        [reaction.id for reaction in source.boundary]
        if scaffold
        else list(source.medium)
    )
    for source_id in boundary_source_ids:
        source_exchange = source.reactions.get_by_id(source_id)
        stoich = {met_map[m.id]: c for m, c in source_exchange.metabolites.items()}
        key = tuple(sorted((met.id, c) for met, c in stoich.items()))
        target_id = (
            source_id
            if source_id not in universal.reactions
            else f"{reference_prefix}{source_id}"
        )
        if key in exchange_equations:
            if scaffold:
                target = universal.reactions.get_by_id(exchange_equations[key])
                target.notes["reference_reaction"] = source_id
                target.notes["evidence_status"] = "public_reference_scaffold"
                reference_exchanges[source_id] = {
                    "source": source_id,
                    "target_id": exchange_equations[key],
                    "existing_equation": True,
                }
            continue
        rid = target_id
        reaction = Reaction(rid, name=source_exchange.name)
        reaction.bounds = (0.0, max(0.0, source_exchange.upper_bound))
        reaction.add_metabolites(stoich)
        reaction.notes["reference_boundary"] = source_id
        pending_reactions.append(reaction)
        pending_ids.add(rid)
        reaction.notes["reference_reaction"] = source_id
        exchange_equations[key] = rid
        reference_exchanges[source_id] = {"source": source_id, "target_id": rid}

    equation_representatives = {}
    for reaction in universal.reactions:
        key, _ = metabolic_equation_key({m.id: c for m, c in reaction.metabolites.items()})
        equation_representatives.setdefault(key, reaction)
    candidates, metadata = set(), {}
    for source_reaction in source.reactions:
        if scaffold and source_reaction.boundary:
            continue
        if source_reaction.id == source_biomass_id or source_reaction.boundary:
            if not source_reaction.boundary or source_reaction.id.startswith(("EX_", "SK_")):
                continue
            stoich = {
                met_map[m.id]: coefficient for m, coefficient in source_reaction.metabolites.items()
            }
            rid = f"{reference_prefix}{source_reaction.id}"
            if rid in universal.reactions:
                continue
            reaction = Reaction(rid, name=source_reaction.name)
            reaction.bounds = source_reaction.bounds
            reaction.add_metabolites(stoich)
            reaction.notes["reference_reaction"] = source_reaction.id
            reaction.notes["evidence_status"] = "public_reference_demand_candidate"
            pending_reactions.append(reaction)
            pending_ids.add(rid)
            candidates.add(rid)
            metadata[rid] = {
                "source_reaction": source_reaction.id,
                "gpr": "",
                "status": "reference_demand_candidate",
            }
            continue
        unverified_reference = any(
            not met.formula or met.charge is None for met in source_reaction.metabolites
        )
        if (
            unverified_reference
            and not config.get("allow_unverified_gapfill", False)
            and not scaffold
        ):
            continue
        try:
            if (
                not scaffold
                and not unverified_reference
                and source_reaction.check_mass_balance()
            ):
                continue
        except (TypeError, ValueError):
            continue
        stoich = {
            met_map[m.id]: coefficient for m, coefficient in source_reaction.metabolites.items()
        }
        key, _ = metabolic_equation_key({m.id: c for m, c in stoich.items()})
        native = (
            universal.reactions.get_by_id(source_reaction.id)
            if source_reaction.id in universal.reactions
            else None
        )
        if native is not None:
            native_key, _ = metabolic_equation_key(
                {met.id: coefficient for met, coefficient in native.metabolites.items()}
            )
            if native_key == key:
                compiled, _ = compile_gpr(
                    source_reaction.gene_reaction_rule,
                    reference_genes,
                    evidence,
                    proteins,
                    config,
                )
                candidates.add(native.id)
                metadata[native.id] = {
                    "source_reaction": source_reaction.id,
                    "gpr": compiled,
                    "bounds": list(source_reaction.bounds),
                    "status": (
                        "public_reference_unverified"
                        if unverified_reference
                        else "reference_candidate_existing_union"
                    ),
                }
                continue
            native_stoich = {met.id: coefficient for met, coefficient in native.metabolites.items()}
            source_stoich = {met.id: coefficient for met, coefficient in stoich.items()}
            same_non_proton_chemistry = _non_proton_stoichiometry(
                native_stoich
            ) == _non_proton_stoichiometry(source_stoich)
            if source_reaction.id == native.id and same_non_proton_chemistry:
                # Keep the operational-library equation when a reference model
                # differs only in protonation.  Importing the raw reference
                # equation would create a second pathway that makes or
                # consumes net protons under closed-boundary QC.
                native.notes["reference_chemistry_conflict"] = json.dumps(
                    {
                        "source_reaction": source_reaction.id,
                        "source_equation": source_reaction.reaction,
                        "native_equation": native.reaction,
                        "resolution": "preserve_operational_library_equation",
                    },
                    sort_keys=True,
                )
                compiled, _ = compile_gpr(
                    source_reaction.gene_reaction_rule,
                    reference_genes,
                    evidence,
                    proteins,
                    config,
                )
                candidates.add(native.id)
                metadata[native.id] = {
                    "source_reaction": source_reaction.id,
                    "gpr": compiled,
                    "bounds": list(source_reaction.bounds),
                    "status": "reference_candidate_native_equation_protonation_normalized",
                }
                continue
        existing = equation_representatives.get(key)
        if existing is not None and existing.id.startswith(reference_prefix):
            same_direction = (
                source_reaction.upper_bound <= 1e-12 or existing.upper_bound > 1e-12
            ) and (source_reaction.lower_bound >= -1e-12 or existing.lower_bound < -1e-12)
            if same_direction:
                continue
        rid = f"{reference_prefix}{source_reaction.id}"
        if rid in universal.reactions:
            continue
        reaction = Reaction(rid, name=source_reaction.name)
        reaction.bounds = source_reaction.bounds
        reaction.add_metabolites(stoich)
        try:
            if (
                not scaffold
                and not unverified_reference
                and reaction.check_mass_balance()
            ):
                continue
        except (TypeError, ValueError):
            continue
        compiled, _ = compile_gpr(
            source_reaction.gene_reaction_rule,
            reference_genes,
            evidence,
            proteins,
            config,
        )
        reaction.gene_reaction_rule = compiled
        reaction.notes["reference_reaction"] = source_reaction.id
        reaction.notes["evidence_status"] = (
            "public_reference_gpr" if compiled else "public_reference_no_gpr"
        )
        pending_reactions.append(reaction)
        pending_ids.add(rid)
        equation_representatives[key] = reaction
        candidates.add(rid)
        metadata[rid] = {
            "source_reaction": source_reaction.id,
            "gpr": compiled,
            "bounds": list(source_reaction.bounds),
            "status": (
                "public_reference_unverified" if unverified_reference else "reference_candidate"
            ),
        }
    universal.add_reactions(pending_reactions)
    return candidates, metadata, reference_exchanges, met_map
