"""Deterministic gene and SBO annotation for reconstructed models."""

from __future__ import annotations

import re


def metabolic_attach_gene_annotations(
    model, evidence: list[dict], proteins: list[tuple[str, str]], gene_ids: dict[str, str]
) -> dict[str, int]:
    """Annotate GPR genes and remove gene products absent from all GPRs."""
    from cobra.manipulation import remove_genes

    gpr_ids = {gene_id for reaction in model.reactions for gene_id in reaction.gpr.genes}
    orphans = [gene for gene in model.genes if gene.id not in gpr_ids]
    if orphans:
        remove_genes(model, orphans, remove_reactions=False)

    by_model_id: dict[str, dict] = {}
    by_input: dict[str, dict] = {}
    for row in evidence:
        model_id = str(row.get("gene_id", ""))
        input_id = str(row.get("input_gene_id", ""))
        if model_id:
            by_model_id[model_id] = row
        if input_id:
            by_input[input_id] = row

    def gene_annotation(input_id: str, row: dict | None) -> dict:
        row = row or {}
        protein_ids = row.get("protein_id", [])
        if isinstance(protein_ids, str):
            protein_ids = [protein_ids]
        protein_id = next((str(value) for value in protein_ids if value), input_id)
        source = str(row.get("source", "NCBI"))
        annotation = {
            "sbo": "SBO:0000243",
            "source": source,
            "evidence_source": source,
        }
        if re.fullmatch(
            r"(?:AC|AP|NC|NG|NM|NP|NR|NT|NW|XM|XP|XR|YP|ZP)_\d+(?:\.\d+)?",
            protein_id,
        ):
            annotation["refseq"] = protein_id
            annotation["ncbiprotein"] = protein_id
        elif protein_id:
            annotation["ncbiprotein"] = protein_id
        reference_id = str(row.get("reference_id", ""))
        if reference_id:
            annotation["asap"] = reference_id
        for value in row.get("db_xref", []) or []:
            value = str(value)
            match = re.search(r"(?:GeneID|geneid):(\d+)", value)
            if match:
                annotation["ncbigene"] = match.group(1)
            match = re.search(r"UniProtKB(?:/Swiss-Prot|/TrEMBL):([A-Z0-9]+)", value)
            if match:
                annotation["uniprot"] = match.group(1)
            match = re.search(r"ECOCYC:(EG\d+)", value, re.I)
            if match:
                annotation["ecogene"] = match.group(1).upper()
        if row.get("gene_symbol"):
            annotation["gene_symbol"] = row["gene_symbol"]
        if row.get("product"):
            annotation["product"] = row["product"]
        if row.get("ec"):
            annotation["ec-code"] = sorted(set(row["ec"]))
        if source == "CLEAN":
            if row.get("confidence") is not None:
                annotation["clean_score"] = float(row["confidence"])
            elif row.get("distance") is not None:
                annotation["clean_score"] = float(row["distance"])
            if row.get("clean_score_kind"):
                annotation["clean_score_kind"] = str(row["clean_score_kind"])
        return annotation

    annotated = 0
    for input_id, _sequence in proteins:
        model_id = gene_ids.get(input_id)
        if model_id not in gpr_ids or not model.genes.has_id(model_id):
            continue
        row = by_model_id.get(model_id) or by_input.get(input_id)
        gene = model.genes.get_by_id(model_id)
        gene.annotation.update(gene_annotation(input_id, row))
        if row and row.get("gene_symbol"):
            gene.name = row["gene_symbol"]
        elif row and row.get("product"):
            gene.name = row["product"]
        annotated += bool(gene.annotation)
    return {
        "annotated_genes": annotated,
        "orphan_genes_removed": len(orphans),
        "input_genes_without_gpr": len(set(gene_ids.values()) - gpr_ids),
        "gpr_genes": len(gpr_ids),
        "total_gene_products": len(model.genes),
    }


def metabolic_enrich_memote_annotations(model) -> dict[str, int]:
    """Apply portable metabolite and reaction SBO annotations."""
    for metabolite in model.metabolites:
        metabolite.annotation.setdefault("sbo", "SBO:0000247")

    counts = {"metabolites": len(model.metabolites), "reactions": 0}
    for reaction in model.reactions:
        if reaction.boundary:
            if reaction.id.startswith("EX_"):
                sbo = "SBO:0000627"
            elif reaction.id.startswith("SK_"):
                sbo = "SBO:0000632"
            elif reaction.id.startswith("DM_"):
                sbo = "SBO:0000628"
            else:
                sbo = "SBO:0000627"
        elif "biomass" in reaction.id.lower() or "biomass" in reaction.name.lower():
            sbo = "SBO:0000629"
        else:
            reactants = {
                metabolite.id.rsplit("_", 1)[0]
                for metabolite in reaction.reactants
                if metabolite.id.rsplit("_", 1)[-1] not in {"e", "p", "c"}
            }
            products = {
                metabolite.id.rsplit("_", 1)[0]
                for metabolite in reaction.products
                if metabolite.id.rsplit("_", 1)[-1] not in {"e", "p", "c"}
            }
            is_transport = "transport" in reaction.name.lower() or (
                bool(reactants) and reactants == products
            )
            sbo = "SBO:0000185" if is_transport else "SBO:0000176"
        reaction.annotation.setdefault("sbo", sbo)
        counts["reactions"] += 1
    return counts


__all__ = ["metabolic_attach_gene_annotations", "metabolic_enrich_memote_annotations"]
