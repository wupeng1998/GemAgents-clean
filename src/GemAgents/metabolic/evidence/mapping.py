"""Deterministic reaction/evidence mapping and provenance ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from GemAgents.metabolic.ec import (
    metabolic_ec_matches,
    metabolic_normalize_ec_values,
)


def metabolic_map_evidence(
    model: Any,
    evidence: list[dict],
    *,
    allow_ambiguous_gpr: bool = False,
) -> dict[str, dict]:
    """EC-to-reaction candidate mapping with ambiguity kept explicit.

    By default an EC hit contributes a GPR gene only when the EC resolves to a
    single reaction. ``allow_ambiguous_gpr`` is an explicit exploratory mode:
    it records every candidate protein as an OR rule while retaining
    ``ambiguous=True`` and the candidate list in the evidence relation.
    """
    by_ec: dict[str, list] = {}
    for reaction in model.reactions:
        values = metabolic_normalize_ec_values(reaction.annotation.get("ec-code", []))
        for ec in values:
            if ec:
                by_ec.setdefault(ec, []).append(reaction)
    mapped: dict[str, dict] = {}
    for row in evidence:
        for ec in metabolic_normalize_ec_values(row.get("ec", [])):
            candidates = [
                reaction
                for model_ec, reactions in by_ec.items()
                if metabolic_ec_matches(ec, model_ec)
                for reaction in reactions
            ]
            candidates = sorted(
                {reaction.id: reaction for reaction in candidates}.values(),
                key=lambda item: item.id,
            )
            for reaction in candidates:
                entry = mapped.setdefault(
                    reaction.id,
                    {
                        "genes": [],
                        "gpr_genes": [],
                        "ec": [],
                        "sources": [],
                        "score": 0.0,
                        "score_kind": "candidate_specificity_weight_not_probability",
                        "ambiguous": False,
                        "relations": [],
                    },
                )
                entry["genes"].append(row["gene_id"])
                entry["ec"].append(ec)
                entry["sources"].append(row.get("source", "unknown"))
                entry["score"] = max(entry["score"], 1.0 / len(candidates))
                entry["ambiguous"] |= len(candidates) != 1
                entry["relations"].append(
                    {
                        "input_gene_id": row["gene_id"],
                        "sequence_sha256": row.get("sequence_sha256"),
                        "function_id": ec,
                        "reaction_id": reaction.id,
                        "annotation_route": row.get("source", "unresolved"),
                        "database_version": row.get("database_version", "unresolved"),
                        "thresholds": {
                            key: row[key]
                            for key in ("sequence_cutoff", "domain_cutoff")
                            if row.get(key) is not None
                        },
                        "coverage": row.get("coverage"),
                        "ambiguity": sorted(candidate.id for candidate in candidates),
                        "complex_status": row.get("complex_status", "unresolved"),
                        "compartment_support": row.get("compartment_support", "unresolved"),
                        "rejection_reason": (
                            "ec_maps_to_multiple_reactions" if len(candidates) != 1 else None
                        ),
                    }
                )
                # EC-only hits are candidate support. Never invent AND complexes.
                # In the default policy, one EC mapping to multiple substrates
                # remains candidate-only; the opt-in mode below records an
                # explicitly ambiguous OR rule for exploratory coverage.
                if row.get("complex_status") != "unresolved" and (
                    len(candidates) == 1 or allow_ambiguous_gpr
                ):
                    entry["gpr_genes"].append(row["gene_id"])
                    if len(candidates) != 1:
                        entry["gpr_ambiguity"] = "ec_candidate_or"
    for entry in mapped.values():
        for key in ("genes", "gpr_genes", "ec", "sources"):
            entry[key] = sorted(set(entry[key]))
    return mapped


def metabolic_mapping_audit(model: Any, evidence: list[dict], mapped: dict[str, dict]) -> dict:
    """Summarize where EC evidence is lost before reconstruction.

    The report separates proteins with no reaction for any EC from proteins
    whose ECs are multiply mapped. It is diagnostic metadata and never changes
    the mapping policy or solver candidate pool.
    """
    by_ec: dict[str, list[str]] = {}
    for reaction in model.reactions:
        values = metabolic_normalize_ec_values(reaction.annotation.get("ec-code", []))
        for ec in values:
            by_ec.setdefault(ec, []).append(reaction.id)
    gene_status: dict[str, dict[str, bool]] = {}
    relation_counts = {"no_reaction": 0, "unique": 0, "ambiguous": 0}
    source_counts: dict[str, int] = {}
    for row in evidence:
        ecs = metabolic_normalize_ec_values(row.get("ec", []))
        gene = str(row.get("input_gene_id") or row.get("gene_id") or "")
        if not ecs or not gene:
            continue
        status = gene_status.setdefault(
            gene, {"mapped": False, "unique": False, "ambiguous": False}
        )
        source = str(row.get("source", "unknown"))
        source_counts[source] = source_counts.get(source, 0) + 1
        for ec in ecs:
            candidates = sorted(
                {
                    reaction_id
                    for model_ec, reaction_ids in by_ec.items()
                    if metabolic_ec_matches(str(ec), model_ec)
                    for reaction_id in reaction_ids
                }
            )
            if not candidates:
                relation_counts["no_reaction"] += 1
            elif len(candidates) == 1:
                relation_counts["unique"] += 1
                status["mapped"] = status["unique"] = True
            else:
                relation_counts["ambiguous"] += 1
                status["mapped"] = status["ambiguous"] = True
    no_reaction_genes = sum(not row["mapped"] for row in gene_status.values())
    ambiguous_only_genes = sum(
        row["ambiguous"] and not row["unique"] for row in gene_status.values()
    )
    return {
        "evidence_rows_with_ec": sum(bool(row.get("ec")) for row in evidence),
        "evidence_genes_with_ec": len(gene_status),
        "mapped_reactions": len(mapped),
        "mapped_reactions_ambiguous": sum(
            bool(row.get("ambiguous")) for row in mapped.values()
        ),
        "mapped_gpr_genes": len(
            {gene for row in mapped.values() for gene in row.get("gpr_genes", [])}
        ),
        "relations": relation_counts,
        "genes_without_any_reaction": no_reaction_genes,
        "genes_with_ambiguous_only": ambiguous_only_genes,
        "sources": source_counts,
        "policy": "candidate_or_enabled" if any(
            row.get("gpr_ambiguity") == "ec_candidate_or" for row in mapped.values()
        ) else "strict_unique_ec_gpr",
    }


def metabolic_evidence_status(entry: dict | None) -> str:
    """Keep reaction provenance explicit when NCBI and CLEAN evidence mix."""
    sources = set(entry.get("sources", [])) if entry else set()
    has_clean = "CLEAN" in sources
    has_ncbi = bool(sources - {"CLEAN"})
    if has_clean and has_ncbi:
        return "NCBI_and_CLEAN_functional_evidence"
    if has_clean:
        return "CLEAN_functional_prediction"
    if has_ncbi:
        return "NCBI_functional_evidence"
    return "template_or_gapfill; no sequence evidence asserted"


def metabolic_reaction_evidence_ledger(model: Any) -> list[dict]:
    """Bind every exported reaction to evidence without upgrading solver picks."""
    rows = []
    for reaction in sorted(model.reactions, key=lambda item: item.id):
        status = str(reaction.notes.get("evidence_status", "unknown"))
        folded = status.casefold()
        if "ncbi" in folded or "clean" in folded:
            evidence_class = "sequence_evidence"
        elif "public_reference" in folded or "public_bigg_gapfill" in folded:
            evidence_class = "reference_gapfill"
        elif any(token in folded for token in ("template", "biomass", "medium_support")):
            evidence_class = "template_support"
        elif "boundary" in folded:
            evidence_class = "public_database_record"
        else:
            evidence_class = "unknown_chemistry"
        sources = []
        for key in ("library_sources", "public_bigg_sources"):
            value = reaction.notes.get(key)
            if not value:
                continue
            try:
                decoded = json.loads(value) if isinstance(value, str) else value
            except (TypeError, ValueError):
                decoded = {"unparsed": str(value)}
            sources.append({"field": key, "value": decoded})
        identity = {
            "reaction_id": reaction.id,
            "evidence_class": evidence_class,
            "evidence_status": status,
            "sources": sources,
        }
        evidence_id = (
            "evidence:"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        selected_by_solver = reaction.notes.get("gapfill") == "true"
        reaction.notes["evidence_id"] = evidence_id
        reaction.notes["evidence_class"] = evidence_class
        rows.append(
            {
                **identity,
                "evidence_id": evidence_id,
                "selected_by_solver": selected_by_solver,
                "solver_selection_upgrades_evidence": False,
            }
        )
    return rows
