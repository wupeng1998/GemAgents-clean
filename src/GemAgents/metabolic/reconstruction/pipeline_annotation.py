"""Annotation, clean-input and reaction-mapping pipeline stage."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from GemAgents.contracts import DEFAULT_ALLOW_AMBIGUOUS_EC_GPR
from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout


def summarize_evidence_mapping(evidence: list[dict], mapped: dict[str, dict]) -> dict:
    """Summarize the evidence funnel without upgrading ambiguous mappings.

    ``proteins_with_ec`` is an annotation-stage count, whereas GPR genes are
    a reaction-selection count.  Keeping these populations separate in the
    manifest makes losses caused by missing EC mappings, EC ambiguity, and
    later model selection explicit instead of presenting them as one count.
    """
    annotation_genes = {str(row.get("gene_id")) for row in evidence if row.get("gene_id")}
    annotation_ec_genes = {
        str(row.get("gene_id"))
        for row in evidence
        if row.get("gene_id") and row.get("ec")
    }
    mapped_genes = {
        str(gene)
        for entry in mapped.values()
        for gene in entry.get("genes", [])
        if gene
    }
    unique_reaction_genes = {
        str(gene)
        for entry in mapped.values()
        if not entry.get("ambiguous", False)
        for gene in entry.get("genes", [])
        if gene
    }
    gpr_genes = {
        str(gene)
        for entry in mapped.values()
        for gene in entry.get("gpr_genes", [])
        if gene
    }
    source_counts = {}
    source_rows: dict[str, list[dict]] = {}
    gene_sources: dict[str, set[str]] = {}
    for row in evidence:
        source = str(row.get("source", "unknown"))
        source_counts[source] = source_counts.get(source, 0) + 1
        source_rows.setdefault(source, []).append(row)
        if row.get("gene_id"):
            gene_sources.setdefault(str(row["gene_id"]), set()).add(source)

    # Keep the same funnel broken down by annotation route.  Mapping entries
    # retain relation-level routes, so a gene is attributed to the source that
    # actually supplied its EC relation even when a reaction mixes NCBI and
    # CLEAN evidence.  Small unit fixtures may omit relations; in that case
    # the entry-level ``sources``/``genes`` fields are the declared fallback.
    source_mapped_genes: dict[str, set[str]] = {}
    source_unique_genes: dict[str, set[str]] = {}
    source_gpr_genes: dict[str, set[str]] = {}
    source_reactions: dict[str, set[str]] = {}
    source_ambiguous_reactions: dict[str, set[str]] = {}
    source_gpr_reactions: dict[str, set[str]] = {}
    for reaction_id, entry in mapped.items():
        relations = entry.get("relations") or []
        relation_sources: dict[str, set[str]] = {}
        for relation in relations:
            source = str(relation.get("annotation_route", "unknown"))
            gene = str(relation.get("input_gene_id", ""))
            if gene:
                relation_sources.setdefault(source, set()).add(gene)
        declared_sources = {str(source) for source in entry.get("sources", [])}
        if relation_sources:
            sources = set(relation_sources)
        else:
            sources = {
                source
                for gene in entry.get("genes", [])
                for source in gene_sources.get(str(gene), set())
            }
            sources |= declared_sources
            if not sources:
                sources = {"unknown"}
        for source in sources:
            genes = relation_sources.get(source, set(entry.get("genes", [])))
            gpr_genes_for_source = (
                genes & set(entry.get("gpr_genes", []))
                if relation_sources
                else set(entry.get("gpr_genes", []))
            )
            source_reactions.setdefault(source, set()).add(reaction_id)
            source_mapped_genes.setdefault(source, set()).update(genes)
            if not entry.get("ambiguous", False):
                source_unique_genes.setdefault(source, set()).update(genes)
            if entry.get("gpr_rule") or entry.get("gpr_genes"):
                source_gpr_reactions.setdefault(source, set()).add(reaction_id)
                source_gpr_genes.setdefault(source, set()).update(gpr_genes_for_source)
            if entry.get("ambiguous", False):
                source_ambiguous_reactions.setdefault(source, set()).add(reaction_id)

    by_source = {}
    for source, rows in sorted(source_rows.items()):
        annotation_genes_for_source = {
            str(row.get("gene_id")) for row in rows if row.get("gene_id")
        }
        annotation_ec_genes_for_source = {
            str(row.get("gene_id"))
            for row in rows
            if row.get("gene_id") and row.get("ec")
        }
        mapped_genes_for_source = source_mapped_genes.get(source, set())
        unique_genes_for_source = source_unique_genes.get(source, set())
        by_source[source] = {
            "annotation_rows": len(rows),
            "annotation_genes": len(annotation_genes_for_source),
            "annotation_genes_with_ec": len(annotation_ec_genes_for_source),
            "mapped_candidate_reactions": len(source_reactions.get(source, set())),
            "mapped_candidate_genes": len(mapped_genes_for_source),
            "mapped_genes_with_unique_reaction": len(unique_genes_for_source),
            "mapped_genes_only_ambiguous": len(mapped_genes_for_source - unique_genes_for_source),
            "unmapped_annotation_ec_genes": len(
                annotation_ec_genes_for_source - mapped_genes_for_source
            ),
            "ambiguous_candidate_reactions": len(
                source_ambiguous_reactions.get(source, set())
            ),
            "strict_gpr_reactions": len(source_gpr_reactions.get(source, set())),
            "strict_gpr_genes": len(source_gpr_genes.get(source, set())),
        }
    return {
        "annotation_rows": len(evidence),
        "annotation_genes": len(annotation_genes),
        "annotation_genes_with_ec": len(annotation_ec_genes),
        "mapped_candidate_reactions": len(mapped),
        "mapped_candidate_genes": len(mapped_genes),
        "mapped_genes_with_unique_reaction": len(unique_reaction_genes),
        "mapped_genes_only_ambiguous": len(mapped_genes - unique_reaction_genes),
        "unmapped_annotation_ec_genes": len(annotation_ec_genes - mapped_genes),
        "ambiguous_candidate_reactions": sum(
            bool(entry.get("ambiguous", False)) for entry in mapped.values()
        ),
        "strict_gpr_reactions": sum(
            bool(entry.get("gpr_rule") or entry.get("gpr_genes"))
            for entry in mapped.values()
        ),
        "strict_gpr_genes": len(gpr_genes),
        "evidence_rows_by_source": dict(sorted(source_counts.items())),
        "by_source": by_source,
    }


def run_annotation_and_mapping(
    *,
    config: dict,
    workspace: Path,
    input_path: Path,
    out: Path,
    manifest: dict,
    cpus: int,
    phase,
    detect_input_fn,
    run_pgap_fn,
    import_ncbi_fn,
    predict_genes_fn,
    write_fasta_fn,
    fasta_fn,
    hash_path,
    hmm_annotate_fn,
    prepare_clean_inputs_fn,
    import_clean_predictions_fn,
    write_json_fn,
    native_universe_fn,
    universe_fn,
    map_evidence_fn,
    mapping_audit_fn=None,
    copy_file_fn=shutil.copyfile,
) -> dict[str, Any]:
    """Run deterministic annotation and mapping without constructing a model."""
    kind, input_detection = detect_input_fn(input_path, config.get("input_type", "auto"))
    config["input_type"] = kind
    manifest["input_detection"] = input_detection
    annotation = config.get("annotation", "auto")
    if annotation == "auto":
        annotation = (
            "ncbi-import"
            if config.get("annotation_gbk") or config.get("pgap_output")
            else ("pgap" if kind == "fna" else "ncbi-hmm")
        )
    manifest["annotation_method"] = annotation
    phase("annotation")
    if annotation in {"pgap", "ncbi-import"}:
        if annotation == "pgap":
            if kind != "fna":
                raise ToolError(
                    "Full PGAP requires a nucleotide genome; FAA uses ncbi-hmm or ncbi-import"
                )
            faa = run_pgap_fn(input_path, out, config, workspace)
        else:
            faa = (
                Path(config["annotation_gbk"])
                if config.get("annotation_gbk")
                else (Path(config["pgap_output"]) / "annot.gbk")
            )
        manifest["annotation_file_sha256"] = hash_path(faa)
        faa, evidence = import_ncbi_fn(input_path, kind, faa, out)
    elif annotation in {"ncbi-hmm", "pyrodigal-ncbi-hmm"}:
        if kind == "fna":
            if annotation != "pyrodigal-ncbi-hmm":
                raise ToolError("Raw FNA requires PGAP or the explicit pyrodigal-ncbi-hmm route")
            faa = predict_genes_fn(input_path, out, int(config.get("genetic_code", 11)))
        else:
            faa = out / "proteins.faa"
            write_fasta_fn(fasta_fn(input_path, "faa"), faa)
        directory = Path(config.get("hmm_dir", RepoLayout(workspace).assets / "ncbi_hmm"))
        index = json.loads((directory / "enzyme-index.json").read_text(encoding="utf-8"))
        if hash_path(directory / "enzymes.hmm") != index["library_sha256"]:
            raise ToolError("NCBI HMM library checksum mismatch; rebuild the reference cache")
        manifest["ncbi_hmm_reference"] = index
        evidence = hmm_annotate_fn(faa, directory, cpus, out / "annotation.json")
    else:
        raise ToolError(f"Unknown annotation route: {annotation}")

    proteins = fasta_fn(faa, "faa")
    clean_input_ids, gene_ids, _clean_rows, ncbi_ec_ids = prepare_clean_inputs_fn(
        config=config,
        out=out,
        proteins=proteins,
        evidence=evidence,
        manifest=manifest,
        phase=phase,
        write_fasta=write_fasta_fn,
        import_clean_predictions=import_clean_predictions_fn,
        write_json=write_json_fn,
    )
    manifest["proteins"] = len(proteins)
    manifest["proteins_sha256"] = hash_path(faa)
    manifest["ncbi_proteins_with_ec"] = len(ncbi_ec_ids)
    manifest["proteins_with_ec"] = len(ncbi_ec_ids)
    manifest["proteins_with_any_ec"] = len(
        {str(row["gene_id"]) for row in evidence if row.get("ec")}
    )

    phase("reaction_mapping")
    universal, universe_path = (
        native_universe_fn(config) if config.get("engine") == "native" else universe_fn(config)
    )
    manifest["universe_sha256"] = hash_path(universe_path)
    if config.get("reaction_library"):
        library_manifest = Path(config["reaction_library"]) / "manifest.json"
        if not library_manifest.is_file():
            raise ToolError(f"Reaction library manifest is missing: {library_manifest}")
        # A batch worker may be resumed after an external cleanup of its
        # output directory.  Recreate the destination before copying so the
        # failure identifies the missing source asset rather than leaking a
        # low-level shutil FileNotFoundError for the destination path.
        out.mkdir(parents=True, exist_ok=True)
        copy_file_fn(library_manifest, out / "library-manifest.json")
        manifest["reaction_library_manifest_sha256"] = hash_path(library_manifest)
    if config.get("modelseed_reactions"):
        manifest["reaction_mapping_reference"] = {
            "sha256": hash_path(Path(config["modelseed_reactions"])),
            "mapping": "ModelSEED reaction ID to complete EC; database versions may differ",
        }
    if config.get("allow_ambiguous_ec_gpr", DEFAULT_ALLOW_AMBIGUOUS_EC_GPR):
        mapped = map_evidence_fn(universal, evidence, allow_ambiguous_gpr=True)
    else:
        mapped = map_evidence_fn(universal, evidence)
    write_json_fn(out / "reaction_evidence.json", mapped)
    if mapping_audit_fn is not None:
        mapping_audit = mapping_audit_fn(universal, evidence, mapped)
        write_json_fn(out / "mapping-audit.json", mapping_audit)
        manifest["mapping_audit"] = mapping_audit
    manifest["candidate_reactions"] = len(mapped)
    manifest["evidence_reconciliation"] = summarize_evidence_mapping(evidence, mapped)
    clean_model_ids = {gene_ids[protein_id] for protein_id in clean_input_ids}
    clean_mapped_ids = {
        gene_id
        for entry in mapped.values()
        if "CLEAN" in entry.get("sources", [])
        for gene_id in entry.get("gpr_genes", [])
        if gene_id in clean_model_ids
    }
    manifest["clean_prediction"]["mapped_genes"] = len(clean_mapped_ids)
    manifest["clean_prediction"]["mapped_reactions"] = sum(
        "CLEAN" in entry.get("sources", []) for entry in mapped.values()
    )
    if manifest["clean_prediction"].get("selected", 0):
        manifest["clean_prediction"]["status"] = (
            "mapped" if clean_mapped_ids else "selected_unmapped"
        )
    manifest["medium_assumption"] = config.get("medium", "minimal")
    return {
        "kind": kind,
        "annotation": annotation,
        "faa": faa,
        "proteins": proteins,
        "evidence": evidence,
        "gene_ids": gene_ids,
        "clean_model_ids": clean_model_ids,
        "universal": universal,
        "universe_path": universe_path,
        "mapped": mapped,
    }
