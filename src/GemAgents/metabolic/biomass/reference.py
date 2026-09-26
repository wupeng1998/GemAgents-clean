"""Reference protein extraction for public biomass templates."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path


def reference_proteins(path: Path) -> list[dict]:
    from Bio import SeqIO

    opener = gzip.open if path.name.endswith(".gz") else open
    result = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for record in SeqIO.parse(handle, "genbank"):
            for feature in record.features:
                if feature.type != "CDS" or not feature.qualifiers.get("translation"):
                    continue
                q = feature.qualifiers
                gene = (q.get("locus_tag") or q.get("protein_id") or [""])[0]
                if not gene:
                    continue
                sequence = q["translation"][0].upper().removesuffix("*")
                result.append(
                    {
                        "gene_id": gene,
                        "gene_symbol": (q.get("gene") or [""])[0],
                        "protein_id": (q.get("protein_id") or [""])[0],
                        "sequence": sequence,
                        "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                        "ec": q.get("EC_number", []),
                    }
                )
    return result
