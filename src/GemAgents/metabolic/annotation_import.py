"""Deterministic GenBank annotation import."""

from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.ec import metabolic_normalize_ec_values
from GemAgents.metabolic.io import metabolic_json
from GemAgents.metabolic.sequence import metabolic_fasta, metabolic_write_fasta


def metabolic_import_ncbi(
    input_path: Path, kind: str, gbk: Path, out: Path
) -> tuple[Path, list[dict]]:
    """Require exact DNA/protein correspondence when importing GenBank CDS evidence."""
    from Bio import SeqIO

    supplied = metabolic_fasta(input_path, kind)
    opener = gzip.open if gbk.name.endswith(".gz") else open
    with opener(gbk, "rt", encoding="utf-8") as handle:
        records = list(SeqIO.parse(handle, "genbank"))
    if not records:
        raise ToolError("No annotated GenBank records")
    if kind == "fna":
        from collections import Counter

        if Counter(seq for _, seq in supplied) != Counter(str(r.seq).upper() for r in records):
            raise ToolError("GenBank annotation does not match the input genome sequences exactly")
    sequence_genes: dict[str, list[str]] = {}
    if kind == "faa":
        for gene, seq in supplied:
            sequence_genes.setdefault(seq, []).append(gene)
    evidence, proteins, matched = [], {}, set()
    for record in records:
        for feature in record.features:
            q = feature.qualifiers
            if (
                feature.type != "CDS"
                or "pseudo" in q
                or "pseudogene" in q
                or not q.get("translation")
            ):
                continue
            translation = q["translation"][0].upper().removesuffix("*")
            reference_id = (q.get("locus_tag") or q.get("protein_id") or [""])[0]
            # One locus can have multiple translated CDS isoforms (e.g. frameshifts).
            # Keep their protein evidence separate instead of merging unlike sequences.
            protein_id = (q.get("protein_id") or [""])[0]
            cds_id = protein_id or f"{record.id}:{reference_id}:{feature.location}"
            genes = sequence_genes.get(translation, []) if kind == "faa" else [cds_id]
            if not genes or not all(genes):
                continue
            for gene in genes:
                matched.add(gene)
                if gene in proteins and proteins[gene] != translation:
                    raise ToolError(f"Conflicting CDS identifiers in annotation: {gene}")
                proteins[gene] = translation
                evidence.append(
                    {
                        "gene_id": gene,
                        "ec": metabolic_normalize_ec_values(q.get("EC_number", [])),
                        "product": (q.get("product") or [""])[0],
                        "gene_symbol": (q.get("gene") or [""])[0],
                        "source": "GenBank_annotation_import",
                        "sequence_sha256": hashlib.sha256(translation.encode()).hexdigest(),
                        "database_version": record.id,
                        "coverage": 1.0,
                        "compartment_support": "unresolved",
                        "record_accession": record.id,
                        "record_comment": record.annotations.get("comment", ""),
                        "reference_id": reference_id,
                        "cds_location": str(feature.location),
                        "protein_id": q.get("protein_id", []),
                        # Keep the original NCBI cross-references so they can
                        # be written to the COBRA gene-product annotation.
                        # In particular, GeneID is needed for MEMOTE's
                        # machine-readable gene annotation checks.
                        "db_xref": q.get("db_xref", []),
                        "inference": q.get("inference", []),
                        "complex_status": "unresolved"
                        if re.search(
                            r"\b(subunit|component|chain|complex)\b",
                            (q.get("product") or [""])[0],
                            re.I,
                        )
                        else "not_inferred",
                    }
                )
    if kind == "faa" and matched != {g for g, _ in supplied}:
        raise ToolError("Some supplied proteins do not exactly match the GenBank CDS translations")
    if not proteins:
        raise ToolError("Annotation contains no matching translated CDS features")
    faa = out / "proteins.faa"
    metabolic_write_fasta(sorted(proteins.items()), faa)
    metabolic_json(out / "annotation.json", evidence)
    return faa, evidence


def metabolic_predict_genes(input_path: Path, out: Path, genetic_code: int) -> Path:
    """Predict proteins with Pyrodigal, explicitly distinct from PGAP."""
    import pyrodigal

    records = metabolic_fasta(input_path, "fna")
    if sum(len(sequence) for _, sequence in records) < 20000:
        raise ToolError(
            "Genome is too short for single-genome training; supply FAA or PGAP results"
        )
    finder = pyrodigal.GeneFinder(meta=False)
    finder.train(
        *(sequence.encode() for _, sequence in records),
        translation_table=genetic_code,
    )
    proteins, coordinates = [], []
    for contig, sequence in records:
        for gene in finder.find_genes(sequence.encode()):
            record_id = f"gene_{len(proteins) + 1:06d}"
            proteins.append(
                (record_id, gene.translate(translation_table=genetic_code).removesuffix("*"))
            )
            coordinates.append(
                {
                    "gene_id": record_id,
                    "contig": contig,
                    "start": gene.begin,
                    "end": gene.end,
                    "strand": gene.strand,
                    "genetic_code": genetic_code,
                    "caller": "Pyrodigal, not PGAP",
                }
            )
    if not proteins:
        raise ToolError("No protein-coding genes predicted")
    faa = out / "proteins.faa"
    metabolic_write_fasta(proteins, faa)
    metabolic_json(out / "gene_coordinates.json", coordinates)
    return faa
