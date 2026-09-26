"""Native reconstruction template and reference-support context setup."""

from __future__ import annotations

import json
import re
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.reconstruction.biomass_support import (
    add_template_support,
)


def prepare_native_reconstruction_context(
    *,
    catalog_path: Path,
    config: dict,
    input_path: Path,
    universal,
    mapped: dict,
    evidence: list[dict],
    out: Path,
    choose_biomass_fn,
    add_reference_support_fn,
    add_biomass_product_drains_fn,
    fasta_fn,
    write_json_fn,
) -> dict:
    """Prepare selected biomass and public-reference context for native build.

    The two callback parameters that select the biomass and add reference
    support remain injectable so the historical ``tools`` facade keeps its
    monkeypatch and dependency-injection behavior.  This helper only
    assembles context; candidate filtering and LP solving stay in their
    reconstruction leaves.
    """
    if not catalog_path.is_file():
        raise ToolError("Native reconstruction requires biomass_library/catalog.json")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    selection_input = input_path
    selection_kind = config.get("input_type", "auto")
    # Biomass selection for a nucleotide input uses the translated protein
    # snapshot produced by annotation.  This keeps the declared DIAMOND policy
    # consistent with FAA requests and avoids trying to compare DNA sketches
    # against protein references.
    if selection_kind == "fna" and (out / "proteins.faa").is_file():
        selection_input = out / "proteins.faa"
        selection_kind = "faa"
    template, biomass_report = choose_biomass_fn(
        catalog, selection_input, selection_kind, config
    )
    biomass_data = template["biomass_reaction"]
    biomass_id = biomass_data["id"]
    (
        template_support_ids,
        template_medium_support_ids,
        template_support_excluded,
        support_aliases,
    ) = add_template_support(universal, template, biomass_report)
    from GemAgents.metabolic.reconstruction.biomass_support import (
        build_biomass_reaction,
        remap_biomass_precursors,
    )
    from GemAgents.metabolic.reconstruction.reference_bridge import (
        resolve_reference_support_path,
    )
    from GemAgents.metabolic.reconstruction.reference_support import (
        apply_template_gprs,
        load_reference_genes,
    )

    biomass, missing_biomass, source_to_canonical = build_biomass_reaction(
        universal, biomass_data, template
    )
    reference = catalog.get("references", {}).get(template["reference_id"], {})
    proteins = dict(fasta_fn(out / "proteins.faa", "faa"))
    reference_genes = {p["gene_id"]: p for p in reference.get("proteins", [])}
    # Public iML1515 JSON assets carry 1,516 b-locus genes but no protein
    # sequences, while the biomass catalog intentionally leaves ``proteins``
    # empty.  Join those genes to the bundled MG1655 FASTA (or an explicitly
    # configured reference FASTA) so exact sequence hashes can compile GPRs.
    reference_support_path = resolve_reference_support_path(catalog_path, config, template)
    loaded_reference_genes = load_reference_genes(
        reference_support_path,
        reference_fasta=config.get("reference_protein_fasta"),
    )
    for gene_id, details in loaded_reference_genes.items():
        reference_genes.setdefault(gene_id, {}).update(
            {key: value for key, value in details.items() if value not in (None, "", [])}
        )
    try:
        gene_id_map = json.loads((out / "gene_id_map.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        gene_id_map = {}
    from collections import defaultdict
    from hashlib import sha256

    input_by_hash = defaultdict(list)
    for input_id, sequence in proteins.items():
        normalized = str(sequence).upper().removesuffix("*")
        input_by_hash[sha256(normalized.encode()).hexdigest()].append(input_id)
    reference_evidence = list(evidence)
    exact_reference_rows = 0
    ambiguous_reference_rows = 0
    reference_mapping_rows = []
    for reference_id, details in reference_genes.items():
        sequence_hash = details.get("sequence_sha256")
        candidates = input_by_hash.get(sequence_hash, []) if sequence_hash else []
        if len(candidates) == 1:
            input_id = candidates[0]
            reference_mapping_rows.append(
                {
                    "reference_gene_id": reference_id,
                    "query_input_id": input_id,
                    "query_model_gene_id": gene_id_map.get(input_id, input_id),
                    "sequence_sha256": sequence_hash,
                    "status": "unique_exact_sequence_hash",
                }
            )
            reference_evidence.append(
                {
                    "gene_id": gene_id_map.get(input_id, input_id),
                    "input_gene_id": input_id,
                    "reference_id": reference_id,
                    "ec": [],
                    "source": "reference_exact_sequence_hash",
                    "complex_status": "not_inferred",
                    "sequence_sha256": sequence_hash,
                }
            )
            exact_reference_rows += 1
        elif len(candidates) > 1:
            ambiguous_reference_rows += 1
            reference_mapping_rows.append(
                {
                    "reference_gene_id": reference_id,
                    "sequence_sha256": sequence_hash,
                    "candidate_query_input_ids": sorted(candidates),
                    "status": "ambiguous_exact_sequence_hash",
                }
            )
        else:
            reference_mapping_rows.append(
                {
                    "reference_gene_id": reference_id,
                    "sequence_sha256": sequence_hash,
                    "status": "missing_exact_sequence_hash",
                }
            )
    rule_details = apply_template_gprs(
        universal,
        template,
        support_aliases,
        reference_genes,
        reference_evidence,
        proteins,
        config,
        mapped,
    )
    reference_prefix = f"REF_{re.sub(r'[^A-Za-z0-9_]+', '_', template['id'])}_"
    (
        reference_candidate_ids,
        reference_candidate_meta,
        reference_exchanges,
        reference_met_map,
    ) = add_reference_support_fn(
        universal,
        reference_support_path,
        reference_genes,
        reference_evidence,
        proteins,
        config,
        reference_prefix,
        biomass_data.get("source_id", biomass_id),
    )
    # Convert compiler output (input FASTA IDs) to the model's deterministic
    # g identifiers before rules enter COBRApy.  Unresolved reference rules
    # remain empty, so old b identifiers can never leak into the draft.
    def model_rule(rule: str) -> str:
        tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_.]*", rule))
        for input_id in sorted(tokens & set(gene_id_map), key=len, reverse=True):
            model_id = gene_id_map[input_id]
            rule = re.sub(
                rf"(?<![A-Za-z0-9_.]){re.escape(input_id)}(?![A-Za-z0-9_.])",
                str(model_id),
                rule,
            )
        return rule

    for entry in mapped.values():
        if entry.get("gpr_rule"):
            entry["gpr_rule"] = model_rule(entry["gpr_rule"])
            entry["gpr_genes"] = sorted(set(re.findall(r"\bg\d+\b", entry["gpr_rule"])))
    for details in reference_candidate_meta.values():
        if details.get("gpr"):
            details["gpr"] = model_rule(details["gpr"])
    # Existing-union reactions receive reference GPRs through metadata rather
    # than a new reaction object.  Merge those rules into the evidence map so
    # initial-model assembly and final gap-fill copies retain the compiled gene
    # rule even when NCBI EC mapping was ambiguous.
    for reaction_id, metadata in reference_candidate_meta.items():
        rule = metadata.get("gpr")
        if not rule:
            continue
        entry = mapped.setdefault(
            reaction_id,
            {
                "genes": [],
                "gpr_genes": [],
                "ec": [],
                "sources": [],
                "score": 1.0,
                "ambiguous": False,
            },
        )
        entry["gpr_rule"] = rule
        entry["gpr_genes"] = sorted(set(re.findall(r"\bg\d+\b", rule)))
        entry["sources"] = sorted(set(entry.get("sources", [])) | {"public_reference"})
        entry["ambiguous"] = False
    missing_biomass = [mid for mid in missing_biomass if mid not in reference_met_map]
    biomass.notes["unmapped_source_metabolites"] = ",".join(sorted(missing_biomass))
    remap_biomass_precursors(
        biomass,
        universal,
        source_to_canonical,
        reference_met_map,
        biomass_report,
    )
    biomass_product_drain_ids = add_biomass_product_drains_fn(universal, biomass)
    biomass_report["biomass_product_drains"] = sorted(biomass_product_drain_ids)
    biomass_report["reference_support"] = {
        "source": str(reference_support_path),
        "candidate_count": len(reference_candidate_ids),
        "exchange_count": len(reference_exchanges),
        "mode": (
            "reference_assisted_scaffold"
            if config.get("reference_scaffold")
            else "chemistry_preserving_candidate_only"
        ),
    }
    biomass_report["reference_gpr_mapping"] = {
        "reference_genes": len(reference_genes),
        "unique_exact_sequence_matches": exact_reference_rows,
        "ambiguous_exact_sequence_matches": ambiguous_reference_rows,
        "source": str(config.get("reference_protein_fasta") or "bundled_carveme_mg1655"),
        "query_gene_ids_are": "deterministic_g_ids_from_gene_id_map",
    }
    write_json_fn(
        out / "reference-gpr-evidence.json",
        {
            "source": str(reference_support_path),
            "reference_protein_fasta": str(
                config.get("reference_protein_fasta") or "bundled_carveme_mg1655"
            ),
            "rows": reference_mapping_rows,
        },
    )
    biomass_report["reference_precursor_debug"] = {
        source_id: {
            "source_in_universal": source_id in universal.metabolites,
            "isolated_in_universal": f"{reference_prefix}{source_id}" in universal.metabolites,
            "mapped_target": reference_met_map.get(source_id).id
            if reference_met_map.get(source_id)
            else None,
            "canonical_in_biomass": any(met.id == canonical_id for met in biomass.metabolites),
        }
        for source_id, canonical_id in source_to_canonical.items()
        if source_id in {"udcpdp_c", "kdo2lipid4_e"}
    }
    write_json_fn(
        out / "biomass-selection.json",
        {**biomass_report, "template": template, "reference_precursor_audit": True},
    )
    return {
        "catalog": catalog,
        "template": template,
        "biomass_report": biomass_report,
        "biomass_data": biomass_data,
        "biomass_id": biomass_id,
        "template_support_ids": template_support_ids,
        "template_medium_support_ids": template_medium_support_ids,
        "template_support_excluded": template_support_excluded,
        "support_aliases": support_aliases,
        "biomass": biomass,
        "missing_biomass": missing_biomass,
        "source_to_canonical": source_to_canonical,
        "proteins": proteins,
        "reference_genes": reference_genes,
        "rule_details": rule_details,
        "reference_support_path": reference_support_path,
        "reference_prefix": reference_prefix,
        "reference_candidate_ids": reference_candidate_ids,
        "reference_candidate_meta": reference_candidate_meta,
        "reference_exchanges": reference_exchanges,
        "reference_met_map": reference_met_map,
        "biomass_product_drain_ids": biomass_product_drain_ids,
    }
