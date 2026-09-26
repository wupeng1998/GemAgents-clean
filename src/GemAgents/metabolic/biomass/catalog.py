"""Pure mapping helpers used while compiling public biomass catalogs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from GemAgents.errors import ToolError


def build_metabolite_index(
    universe, compartment: Callable[[str], str]
) -> dict[tuple[str, str], list[str]]:
    """Index canonical metabolites by identifier/alias and compartment."""
    index: dict[tuple[str, str], list[str]] = {}
    for metabolite in universe.metabolites:
        raw_aliases = metabolite.annotation.get("bigg.metabolite", [])
        raw_aliases = [raw_aliases] if isinstance(raw_aliases, str) else raw_aliases
        for alias in [metabolite.id, *raw_aliases]:
            if alias:
                index.setdefault((alias, compartment(metabolite.compartment)), []).append(
                    metabolite.id
                )
    return index


def map_biomass_stoichiometry(
    biomass,
    universe,
    met_index: dict[tuple[str, str], list[str]],
    explicit_metabolite_map: dict[str, str],
    support_metabolite_ids: set[str],
    compartment: Callable[[str], str],
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """Map a source biomass reaction to canonical IDs without guessing ambiguity."""
    mapped_stoich: dict[str, float] = {}
    mapped_source_ids: dict[str, str] = {}
    unmapped: list[str] = []
    for metabolite, coefficient in biomass.metabolites.items():
        code = compartment(metabolite.compartment)
        explicit_target = explicit_metabolite_map.get(metabolite.id)
        if explicit_target in universe.metabolites:
            mapped_stoich[explicit_target] = float(coefficient)
            mapped_source_ids[metabolite.id] = explicit_target
            continue
        ids = met_index.get((metabolite.id, code), [])
        if len(ids) != 1:
            aliases = metabolite.annotation.get("bigg.metabolite", [])
            aliases = [aliases] if isinstance(aliases, str) else aliases
            ids = [target for alias in aliases for target in met_index.get((alias, code), [])]
        if len(set(ids)) != 1:
            if metabolite.id in support_metabolite_ids:
                mapped_stoich[metabolite.id] = float(coefficient)
                mapped_source_ids[metabolite.id] = metabolite.id
            else:
                unmapped.append(metabolite.id)
        else:
            mapped_stoich[ids[0]] = float(coefficient)
            mapped_source_ids[metabolite.id] = ids[0]
    return mapped_stoich, mapped_source_ids, unmapped


def load_reference_record(
    source: dict,
    workspace: Path,
    *,
    reference_proteins: Callable[[Path], list[dict]],
    fasta_reader: Callable[[Path, str], list[tuple[str, str]]],
    hash_path: Callable[[Path], str],
) -> tuple[dict, list[dict], dict]:
    """Load one declared reference and return its provenance record."""
    reference: dict = {}
    reference_path = source.get("reference_genbank")
    if reference_path:
        reference_path = (workspace / reference_path).resolve()
        proteins = reference_proteins(reference_path)
        reference = {
            "genbank": str(reference_path),
            "genbank_sha256": hash_path(reference_path),
            "proteins": proteins,
        }
    elif source.get("reference_faa"):
        faa = (workspace / source["reference_faa"]).resolve()
        proteins = [
            {
                "gene_id": record_id,
                "sequence": sequence,
                "sequence_sha256": hash_path_from_sequence(sequence),
            }
            for record_id, sequence in fasta_reader(faa, "faa")
        ]
        reference = {
            "faa": str(faa),
            "faa_sha256": hash_path(faa),
            "proteins": proteins,
        }
    else:
        proteins = []
    source_id = str(source.get("id", "")).strip()
    reference_id = f"{source_id}:reference"
    return reference, proteins, {
        **reference,
        "id": reference_id,
        "organism": source.get("organism", source_id),
    }


def build_reference_gpr_rules(model, biomass, universe) -> list[dict[str, str]]:
    """Collect GPR rules whose source reactions exist in the canonical universe."""
    return [
        {"reaction_id": reaction.id, "gene_reaction_rule": reaction.gene_reaction_rule}
        for reaction in model.reactions
        if reaction.id != biomass.id
        and reaction.gene_reaction_rule
        and reaction.id in universe.reactions
    ]


def add_reference_alias_rules(
    model, universe, rules: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Map public source reaction names to balanced canonical reaction IDs."""
    aliases = (
        ("DBTS", "rxn02277_c"),
        ("UPP3MT", "rxn02287_c"),
        ("SHCHF", "rxn02056_c"),
        ("DB4PS", "rxn05040_c"),
        ("RBFSa", "rxn03080_c"),
        ("UDCPDPS", "rxn25383_c"),
    )
    source_rules = {
        reaction.id: reaction.gene_reaction_rule
        for reaction in model.reactions
        if reaction.gene_reaction_rule
    }
    support_aliases: list[dict[str, str]] = []
    for source_id, canonical_id in aliases:
        source_rule = source_rules.get(source_id)
        if not source_rule or canonical_id not in universe.reactions:
            continue
        if not any(row["reaction_id"] == canonical_id for row in rules):
            rules.append({"reaction_id": canonical_id, "gene_reaction_rule": source_rule})
        support_aliases.append(
            {
                "source_reaction_id": source_id,
                "canonical_reaction_id": canonical_id,
                "gene_reaction_rule": source_rule,
                "role": "balanced_modelseed_equivalent",
            }
        )
    return rules, support_aliases


def collect_empirical_assembly_ids(
    model,
    biomass,
    *,
    is_assembly: Callable[[object, set[str]], bool],
    is_pool: Callable[[object, set[str]], bool],
) -> set[str]:
    """Collect explicitly classified biomass assembly reactions by closure."""
    biomass_reactants = {
        metabolite.id
        for metabolite, coefficient in biomass.metabolites.items()
        if coefficient < 0
    }
    assembly_ids = {
        reaction.id
        for metabolite, coefficient in biomass.metabolites.items()
        if coefficient < 0
        for reaction in metabolite.reactions
        if reaction is not biomass and is_assembly(reaction, biomass_reactants)
    }
    assembly_queue = [
        metabolite
        for reaction_id in assembly_ids
        for metabolite, coefficient in model.reactions.get_by_id(reaction_id).metabolites.items()
        if coefficient < 0
    ]
    visited_pool_metabolites: set[str] = set()
    while assembly_queue:
        metabolite = assembly_queue.pop(0)
        if metabolite.id in visited_pool_metabolites:
            continue
        visited_pool_metabolites.add(metabolite.id)
        for reaction in metabolite.reactions:
            if reaction.id in assembly_ids:
                continue
            if not is_pool(reaction, {metabolite.id}):
                continue
            assembly_ids.add(reaction.id)
            assembly_queue.extend(
                item
                for item, coefficient in reaction.metabolites.items()
                if coefficient < 0
            )
    return assembly_ids


def serialize_support_reaction(
    reaction,
    *,
    source_model: str,
    source_model_sha256: str,
    source: str,
    source_url: str,
    support_role: str,
    gene_reaction_rule: str | None = None,
    empirical_pseudoreaction: bool = False,
) -> dict:
    """Serialize one support reaction with stable provenance metadata."""
    payload = {
        "id": reaction.id,
        "name": reaction.name,
        "bounds": [float(reaction.lower_bound), float(reaction.upper_bound)],
        "gene_reaction_rule": (
            reaction.gene_reaction_rule
            if gene_reaction_rule is None
            else gene_reaction_rule
        ),
        "stoichiometry": {
            metabolite.id: {
                "coefficient": float(coefficient),
                "compartment": metabolite.compartment,
                "formula": metabolite.formula,
                "charge": metabolite.charge,
                "annotation": metabolite.annotation,
            }
            for metabolite, coefficient in reaction.metabolites.items()
        },
        "source_model": source_model,
        "source_model_sha256": source_model_sha256,
        "source_reaction_id": reaction.id,
        "source": source,
        "source_url": source_url,
        "support_role": support_role,
    }
    if empirical_pseudoreaction:
        payload["empirical_pseudoreaction"] = True
    return payload


def build_support_reactions(
    model,
    biomass,
    model_path: Path,
    source: dict,
    *,
    hash_path_fn: Callable[[Path], str],
    is_assembly_fn: Callable[[object, set[str]], bool],
    is_pool_fn: Callable[[object, set[str]], bool],
) -> tuple[list[dict], set[str], list[dict[str, str]]]:
    """Serialize public biomass assembly and Fe-S support reactions.

    Only explicitly classified empirical assemblies and the public Fe-S support
    identifiers are admitted.  The helper returns the selected assembly IDs and
    any additional GPR rules so callers can retain the historical catalog shape.
    """
    fe_s_support_ids = {
        "S2FE2SR", "S2FE2SS", "S4FE4SR", "I4FE4SR", "S2FE2ST", "I2FE2SS",
        "I2FE2ST", "I2FE2SS2", "S2FE2SS2", "BTS5", "LIPOS", "I2FE2SR",
        "DBTS", "UPP3MT", "SHCHF", "SHCHD2", "DB4PS", "RBFSa", "RBFSb",
        "RBFK", "MCTP1App", "CPPPGO2", "5DOAN", "MOADSUx", "SCYSDS",
        "ICYSDS", "FESR", "I4FE4ST", "S4FE4ST", "LIPOCT", "LIPAMPL",
        "FESD1s", "FESD2s", "TYRL", "THZPSN3", "OCTNLL",
    }
    source_model = str(model_path)
    source_hash = hash_path_fn(model_path)
    source_url = source.get("source_url", "")
    support_reactions: list[dict] = []
    extra_rules: list[dict[str, str]] = []
    assembly_ids = collect_empirical_assembly_ids(
        model,
        biomass,
        is_assembly=is_assembly_fn,
        is_pool=is_pool_fn,
    )
    for reaction_id in sorted(assembly_ids):
        reaction = model.reactions.get_by_id(reaction_id)
        support = serialize_support_reaction(
            reaction,
            source_model=source_model,
            source_model_sha256=source_hash,
            source="public_biomass_template",
            source_url=source_url,
            support_role="biomass_precursor_assembly",
            gene_reaction_rule="",
            empirical_pseudoreaction=True,
        )
        if reaction.gene_reaction_rule:
            support["source_gene_reaction_rule"] = reaction.gene_reaction_rule
        support_reactions.append(support)
    for reaction in model.reactions:
        if reaction.id not in fe_s_support_ids or reaction.id in assembly_ids:
            continue
        if any(
            not metabolite.formula or metabolite.charge is None
            for metabolite in reaction.metabolites
        ):
            continue
        try:
            if reaction.check_mass_balance():
                continue
        except (TypeError, ValueError):
            continue
        support_reactions.append(
            serialize_support_reaction(
                reaction,
                source_model=source_model,
                source_model_sha256=source_hash,
                source="public_BiGG_iML1515",
                source_url=source_url,
                support_role="iML1515_FeS_maturation",
            )
        )
        if reaction.gene_reaction_rule:
            extra_rules.append(
                {
                    "reaction_id": reaction.id,
                    "gene_reaction_rule": reaction.gene_reaction_rule,
                }
            )
    return support_reactions, assembly_ids, extra_rules


def merge_partial_support(
    partial_cfg: dict,
    workspace: Path,
    support_reactions: list[dict],
    *,
    restricted_path_fn: Callable[[Path], bool],
) -> tuple[list[dict], dict]:
    """Merge an explicitly declared support subset with provenance.

    The helper is deliberately narrow: it only considers reaction IDs listed by
    the caller and refuses policy-restricted paths before opening them.  Existing
    support records are retained and receive an alternate-source record instead
    of being replaced.
    """
    report = {
        "requested": bool(partial_cfg),
        "source": partial_cfg.get("model", ""),
        "added": [],
        "duplicates": [],
        "excluded": [],
    }
    if not partial_cfg:
        return support_reactions, report

    partial_path = (workspace / str(partial_cfg.get("model", ""))).resolve()
    try:
        partial_path.relative_to(workspace.resolve())
    except ValueError as error:
        raise ToolError("alternate source escapes the workspace") from error
    if restricted_path_fn(partial_path):
        raise ToolError("BLOCKED_POLICY: restricted alternate source")
    try:
        partial_data = json.loads(partial_path.read_text(encoding="utf-8"))
        if not isinstance(partial_data, dict) or not isinstance(
            partial_data.get("reactions", []), list
        ):
            raise ValueError("partial source must contain a reactions list")
        allowed = set(partial_cfg.get("reaction_ids", []))
        by_id = {row["id"]: row for row in support_reactions}
        for row in partial_data.get("reactions", []):
            reaction_id = str(row.get("id", ""))
            if allowed and reaction_id not in allowed:
                continue
            if not reaction_id:
                continue
            if row.get("balance_residual"):
                report["excluded"].append(
                    {"reaction": reaction_id, "reason": "source_unbalanced"}
                )
                continue
            provenance = {
                "source": "pear_partial_2fe2s",
                "source_model": partial_data.get("source_model", str(partial_path)),
                "source_model_sha256": partial_data.get("source_model_sha256", ""),
                "source_reaction_id": reaction_id,
                "support_role": "iML1515_FeS_maturation_partial",
            }
            existing = by_id.get(reaction_id)
            if existing is not None:
                existing.setdefault("alternate_sources", []).append(provenance)
                report["duplicates"].append(reaction_id)
                continue
            support = {
                "id": reaction_id,
                "name": row.get("name", reaction_id),
                "bounds": row.get("bounds", [0.0, 1000.0]),
                "gene_reaction_rule": row.get("gene_reaction_rule", ""),
                "stoichiometry": row.get("stoichiometry", {}),
                **provenance,
            }
            support_reactions.append(support)
            by_id[reaction_id] = support
            report["added"].append(reaction_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["excluded"].append(
            {"reason": "partial_source_error", "error": str(error)}
        )
    return support_reactions, report


def hash_path_from_sequence(sequence: str) -> str:
    """Return the stable digest used for inline reference protein sequences."""
    import hashlib

    return hashlib.sha256(sequence.encode()).hexdigest()


__all__ = [
    "build_metabolite_index",
    "add_reference_alias_rules",
    "build_reference_gpr_rules",
    "collect_empirical_assembly_ids",
    "hash_path_from_sequence",
    "load_reference_record",
    "map_biomass_stoichiometry",
    "merge_partial_support",
    "serialize_support_reaction",
]
