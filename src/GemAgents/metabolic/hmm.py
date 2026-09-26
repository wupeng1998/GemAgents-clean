"""NCBI HMM preparation and calibrated enzyme annotation."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash, metabolic_json
from GemAgents.metabolic.network import metabolic_download
from GemAgents.metabolic.sequence import metabolic_fasta


def metabolic_prepare_hmms(directory: Path, *, download: bool = True) -> dict:
    """Build an enzyme-only NCBI HMM library; this is not the complete PGAP pipeline."""
    directory.mkdir(parents=True, exist_ok=True)
    names = ("RELEASE_NOTES.txt", "hmm_PGAP.tsv", "hmm_PGAP.HMM.tgz")
    for name in names:
        path = directory / name
        if not path.is_file():
            if not download:
                raise ToolError(f"Missing NCBI resource {path}; run --prepare-ncbi-hmms")
            metabolic_download(f"https://ftp.ncbi.nlm.nih.gov/hmm/current/{name}", path)
    with (directory / "hmm_PGAP.tsv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    selected = {
        row["#ncbi_accession"]: row
        for row in rows
        if row.get("for_naming") == "Y"
        and re.search(r"\b\d+\.\d+\.\d+\.\d+\b", row.get("ec_numbers", ""))
        and row.get("family_type") == "equivalog"
    }
    if not selected:
        raise ToolError("NCBI metadata has no supported enzyme equivalog models")
    source_hashes = {name: metabolic_hash(directory / name) for name in names}
    index = directory / "enzyme-index.json"
    library = directory / "enzymes.hmm"
    if index.is_file() and library.is_file():
        previous = json.loads(index.read_text(encoding="utf-8"))
        if previous.get("sources") == source_hashes and previous.get(
            "library_sha256"
        ) == metabolic_hash(library):
            return previous
    found = set()
    temporary = directory / "enzymes.hmm.part"
    with temporary.open("wb") as output, tarfile.open(directory / names[2], "r|gz") as archive:
        for member in archive:
            # Read members into one library; never extract archive paths to disk.
            accession = Path(member.name).name.removesuffix(".HMM")
            if not member.isfile() or accession not in selected:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise ToolError(f"Unreadable HMM archive member: {accession}")
            shutil.copyfileobj(handle, output)
            output.write(b"\n")
            found.add(accession)
    if found != selected.keys():
        raise ToolError(f"NCBI metadata/archive mismatch: missing {len(selected.keys() - found)}")
    temporary.replace(library)
    result = {
        "sources": source_hashes,
        "models": len(selected),
        "library_sha256": metabolic_hash(library),
        "release": (directory / names[0]).read_text(encoding="utf-8"),
        "scope": "complete-EC naming equivalogs only; not full PGAP",
    }
    metabolic_json(index, result)
    return result


def metabolic_hmm_annotate(faa: Path, directory: Path, cpus: int, out: Path) -> list[dict]:
    library = directory / "enzymes.hmm"
    if not library.is_file() or not (directory / "enzyme-index.json").is_file():
        raise ToolError("Prepare the NCBI HMM library with --prepare-ncbi-hmms first")
    index = json.loads((directory / "enzyme-index.json").read_text(encoding="utf-8"))
    if (
        metabolic_hash(library) != index["library_sha256"]
        or metabolic_hash(directory / "hmm_PGAP.tsv") != index["sources"]["hmm_PGAP.tsv"]
    ):
        raise ToolError("NCBI HMM reference checksum mismatch; rebuild the reference cache")
    import pyhmmer

    with (directory / "hmm_PGAP.tsv").open(encoding="utf-8") as handle:
        metadata = {row["#ncbi_accession"]: row for row in csv.DictReader(handle, delimiter="\t")}
    records = metabolic_fasta(faa, "faa")
    sequence_by_id = dict(records)
    alphabet = pyhmmer.easel.Alphabet.amino()
    proteins = pyhmmer.easel.DigitalSequenceBlock(
        alphabet,
        [
            pyhmmer.easel.TextSequence(
                name=record_id.encode(), sequence=sequence
            ).digitize(alphabet)
            for record_id, sequence in records
        ],
    )
    evidence = []
    with pyhmmer.plan7.HMMFile(str(library)) as hmms:
        # Set per-model cutoffs from NCBI metadata rather than a generic E-value.
        def calibrated_models():
            for hmm in hmms:
                accession = (
                    hmm.accession.decode() if isinstance(hmm.accession, bytes) else hmm.accession
                )
                row = metadata[accession]
                hmm.cutoffs.gathering = (
                    float(row["sequence_cutoff"]),
                    float(row["domain_cutoff"]),
                )
                yield hmm

        for hits in pyhmmer.hmmsearch(
            calibrated_models(), proteins, cpus=cpus, bit_cutoffs="gathering"
        ):
            accession = hits.query.accession
            accession = accession.decode() if isinstance(accession, bytes) else accession
            row = metadata[accession]
            for hit in hits:
                domains = [
                    domain
                    for domain in hit.domains
                    if domain.score >= float(row["domain_cutoff"])
                ]
                if hit.score < float(row["sequence_cutoff"]) or not domains:
                    continue
                name = hit.name.decode() if isinstance(hit.name, bytes) else hit.name
                evidence.append(
                    {
                        "gene_id": name,
                        "ec": re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", row["ec_numbers"]),
                        "product": row["product_name"],
                        "gene_symbol": row["gene_symbol"],
                        "source": "NCBI_HMM_equivalog",
                        "sequence_sha256": hashlib.sha256(
                            sequence_by_id[name].encode()
                        ).hexdigest(),
                        "database_version": index.get("release", index["library_sha256"]),
                        "accession": accession,
                        "sequence_score": float(hit.score),
                        "sequence_cutoff": float(row["sequence_cutoff"]),
                        "domain_score": float(max(domain.score for domain in domains)),
                        "domain_cutoff": float(row["domain_cutoff"]),
                        "taxonomic_range": row.get("taxonomic_range", ""),
                        "taxonomy_status": "reference_range_recorded_not_used_as_taxonomic_proof",
                        "coverage": None,
                        "compartment_support": "unresolved",
                        "complex_status": "unresolved"
                        if re.search(
                            r"\b(subunit|component|chain|complex)\b",
                            row["product_name"],
                            re.I,
                        )
                        else "not_inferred",
                    }
                )
    metabolic_json(out, evidence)
    return evidence
