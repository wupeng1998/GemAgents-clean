"""Content-aware FASTA parsing used by deterministic metabolic routes."""

from __future__ import annotations

import gzip
from pathlib import Path

from GemAgents.errors import ToolError


def metabolic_fasta(path: Path, kind: str) -> list[tuple[str, str]]:
    """Validate sequence type and unique IDs; extension alone is insufficient."""
    from Bio import SeqIO

    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        records = [(record.id, str(record.seq).upper()) for record in SeqIO.parse(handle, "fasta")]
    if (
        not records
        or any(not record_id for record_id, _ in records)
        or len({record_id for record_id, _ in records}) != len(records)
    ):
        raise ToolError("FASTA must contain sequences with unique, nonempty identifiers")
    allowed = (
        set("ACGTRYSWKMBDHVN")
        if kind == "fna"
        else set("ACDEFGHIKLMNPQRSTVWYBXZJUO")
    )
    normalized = []
    for record_id, sequence in records:
        if kind == "faa":
            sequence = sequence.removesuffix("*")
        if not sequence or set(sequence) - allowed:
            raise ToolError(
                f"Invalid {kind} sequence: {record_id}; internal stops/gaps are not accepted"
            )
        if kind == "faa" and len(sequence) > 150 and set(sequence) <= set("ACGTN"):
            raise ToolError(f"Sequence {record_id} looks like DNA; select fna input explicitly")
        normalized.append((record_id, sequence))
    return normalized


def metabolic_write_fasta(records: list[tuple[str, str]], path: Path) -> None:
    """Write normalized FASTA records with stable UTF-8/newline behavior."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record_id, sequence in records:
            handle.write(f">{record_id}\n{sequence}\n")


def metabolic_detect_input(path: Path, requested: str = "auto") -> tuple[str, str]:
    """Infer FAA/FNA from extension and content, rejecting ambiguous alphabets."""
    from Bio import SeqIO

    if requested not in {"auto", "faa", "fna"}:
        raise ToolError("input_type must be auto, faa or fna")
    suffix = path.name.lower().removesuffix(".gz")
    kind, reason = requested, "explicit input_type"
    if kind == "auto":
        kind = (
            "faa"
            if suffix.endswith((".faa", ".pep"))
            else "fna"
            if suffix.endswith((".fna", ".ffn"))
            else "auto"
        )
        reason = "extension validated against sequence content"
    if kind == "auto":
        opener = gzip.open if path.name.lower().endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as handle:
            sequences = [
                str(record.seq).upper().removesuffix("*")
                for record in SeqIO.parse(handle, "fasta")
            ]
        letters = set("".join(sequences))
        if not letters:
            raise ToolError("Empty FASTA")
        dna = set("ACGTRYSWKMBDHVN")
        amino = set("ACDEFGHIKLMNPQRSTVWYBXZJUO")
        if letters <= dna and sum(
            sequence.count(character)
            for sequence in sequences
            for character in "ACGTN"
        ) >= 0.9 * sum(map(len, sequences)):
            kind = "fna"
        elif letters <= amino and letters - dna:
            kind = "faa"
        else:
            raise ToolError("Ambiguous FASTA alphabet; specify input_type faa or fna")
        reason = "sequence alphabet; ambiguous amino/nucleotide inputs require an override"
    metabolic_fasta(path, kind)
    return kind, reason
