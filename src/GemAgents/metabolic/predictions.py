"""Deterministic import of CLEAN enzyme predictions."""

from __future__ import annotations

import csv
import hashlib
import math
import re
from pathlib import Path

from GemAgents.contracts import DEFAULT_CLEAN_TOP_FRACTION
from GemAgents.errors import ToolError
from GemAgents.metabolic.ec import (
    metabolic_normalize_ec,
)


def clean_prediction_input_ids(path: Path) -> set[str]:
    """Return protein identifiers present in a CLEAN result table.

    This deliberately only inspects the identifier column/field.  It is used
    for provenance checks before score filtering so a result produced for a
    different genome cannot be silently interpreted as an empty prediction.
    """
    if not path.is_file():
        raise ToolError(f"CLEAN predictions do not exist: {path}")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return set()
    delimiter = "\t" if "\t" in lines[0] else ","
    first = next(csv.reader([lines[0]], delimiter=delimiter), [])
    normalized = {re.sub(r"[^a-z0-9]", "", field.casefold()) for field in first}
    has_header = bool(normalized & {
        "proteinid", "geneid", "entry", "ecnumber", "ec", "confidence",
        "score", "distance", "product",
    })
    ids: set[str] = set()
    if has_header:
        rows = csv.DictReader(lines, delimiter=delimiter)
        for row in rows:
            normalized_row = {
                re.sub(r"[^a-z0-9]", "", str(key).casefold()): value
                for key, value in row.items() if key is not None
            }
            identifier = str(
                normalized_row.get("proteinid")
                or normalized_row.get("geneid")
                or normalized_row.get("entry")
                or ""
            ).strip()
            if identifier:
                ids.add(identifier)
    else:
        for fields in csv.reader(lines, delimiter=delimiter):
            if fields and fields[0].strip():
                ids.add(fields[0].strip())
    return ids


def metabolic_import_clean_predictions(
    path: Path,
    proteins: list[tuple[str, str]],
    ncbi_ec_genes: set[str],
    top_fraction: float = DEFAULT_CLEAN_TOP_FRACTION,
) -> list[dict]:
    """Import tabular or official headerless CLEAN output.

    Official output is ``protein,EC:1.2.3.4/score``.  Scores produced by the
    GMM are confidences (larger is better); raw CLEAN distances are smaller is
    better.  The 30% cut is applied after reducing to one prediction per
    protein, so multiple ECs cannot inflate the selected set.
    """
    if (
        isinstance(top_fraction, bool)
        or not isinstance(top_fraction, (int, float))
        or not 0 < top_fraction <= 1
    ):
        raise ToolError("clean_top_fraction must be a number between zero and one")
    if not path.is_file():
        raise ToolError(f"CLEAN predictions do not exist: {path}")

    valid = {str(rid) for rid, _ in proteins} - {str(rid) for rid in ncbi_ec_genes}
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    delimiter = "\t" if "\t" in lines[0] else ","
    first = next(csv.reader([lines[0]], delimiter=delimiter), [])
    normalized = {re.sub(r"[^a-z0-9]", "", field.casefold()) for field in first}
    header_names = {
        "proteinid",
        "geneid",
        "entry",
        "ecnumber",
        "ec",
        "confidence",
        "score",
        "distance",
        "product",
    }
    has_header = bool(normalized & header_names)

    def clean_ec(value: object) -> str:
        return metabolic_normalize_ec(value) or ""

    def token(value: object) -> tuple[str, float | None]:
        value = str(value or "").strip()
        match = re.fullmatch(
            r"((?:EC\s*:\s*)?(?:\d+|[-*])\.(?:\d+|[-*])\.(?:\d+|[-*])\.(?:\d+|[-*]))(?:/([0-9eE+_.-]+))?",
            value,
            re.I,
        )
        if not match:
            return "", None
        try:
            score = float(match.group(2)) if match.group(2) is not None else None
        except ValueError:
            return "", None
        ec = metabolic_normalize_ec(match.group(1))
        return ec or "", score if score is None or math.isfinite(score) else None

    records: list[tuple[str, str, float, str, dict]] = []
    if has_header:
        rows = csv.DictReader(lines, delimiter=delimiter)
        for original in rows:
            row = {
                re.sub(r"[^a-z0-9]", "", str(key).casefold()): value
                for key, value in original.items()
                if key is not None
            }
            pid = str(row.get("proteinid") or row.get("geneid") or row.get("entry") or "").strip()
            if pid not in valid:
                continue
            score_key = next(
                (
                    key
                    for key in ("confidence", "probability", "score", "distance")
                    if row.get(key) not in (None, "")
                ),
                "",
            )
            kind = (
                "confidence"
                if score_key in {"confidence", "probability", "score"}
                else "distance"
                if score_key == "distance"
                else "unknown"
            )
            try:
                explicit_score = float(row[score_key]) if score_key else None
            except (TypeError, ValueError):
                explicit_score = None
            if explicit_score is not None and not math.isfinite(explicit_score):
                continue
            ec_values = re.split(r"[;,\s]+", str(row.get("ecnumber") or row.get("ec") or ""))
            parsed = [token(value) for value in ec_values]
            parsed = [(ec, embedded) for ec, embedded in parsed if ec]
            if not parsed:
                ec = clean_ec(row.get("ecnumber") or row.get("ec"))
                parsed = [(ec, None)] if ec else []
            for ec, embedded in parsed:
                score = explicit_score if explicit_score is not None else embedded
                if score is None:
                    score = 0.0
                records.append((pid, ec, score, kind, original))
    else:
        for fields in csv.reader(lines, delimiter=delimiter):
            if not fields:
                continue
            pid = fields[0].strip()
            if pid not in valid:
                continue
            for field in fields[1:]:
                ec, score = token(field)
                if ec and score is not None:
                    records.append((pid, ec, score, "auto", {"product": ""}))

    if not records:
        return []
    # Headerless official files with GMM confidence are in [0, 1]; raw CLEAN
    # distances are normally >1.  This keeps both official modes importable.
    if all(kind == "auto" for _, _, _, kind, _ in records):
        confidence_mode = all(0 <= score <= 1 for _, _, score, _, _ in records)
        records = [
            (pid, ec, score, "confidence" if confidence_mode else "distance", row)
            for pid, ec, score, _kind, row in records
        ]
    best: dict[str, tuple[str, str, float, str, dict]] = {}
    for record in records:
        pid, ec, score, kind, row = record
        rank = score if kind == "confidence" else -score
        previous = best.get(pid)
        previous_rank = (
            (previous[2] if previous[3] == "confidence" else -previous[2]) if previous else None
        )
        if previous is None or (rank, ec) > (previous_rank, previous[1]):
            best[pid] = record
    ranked = sorted(
        best.values(),
        key=lambda item: (-(item[2] if item[3] == "confidence" else -item[2]), item[0], item[1]),
    )
    keep = math.ceil(len(ranked) * top_fraction)
    selected = []
    sequence_by_id = dict(proteins)
    for pid, ec, score, kind, row in ranked[:keep]:
        item = {
            "gene_id": pid,
            "ec": [ec],
            "product": row.get("product", "") if isinstance(row, dict) else "",
            "source": "CLEAN",
            "sequence_sha256": hashlib.sha256(sequence_by_id[pid].encode()).hexdigest(),
            "database_version": "unresolved",
            "coverage": None,
            "compartment_support": "unresolved",
            "complex_status": "not_inferred",
            "clean_score_kind": kind,
            "selection_fraction": top_fraction,
        }
        item["confidence" if kind == "confidence" else "distance"] = score
        selected.append(item)
    return selected
