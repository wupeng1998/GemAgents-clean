"""Evidence-preserving EC aliases for reaction-library construction."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from GemAgents.metabolic.ec import metabolic_normalize_ec_values


def _field(row: dict[str, str], *names: str) -> str:
    normalized = {
        "".join(char for char in str(key).casefold() if char.isalnum()): value
        for key, value in row.items()
        if key is not None
    }
    for name in names:
        value = normalized.get("".join(char for char in name.casefold() if char.isalnum()))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def read_ec_aliases(path: Path) -> list[dict[str, str]]:
    """Read a CSV/TSV with reaction id and EC columns.

    The parser accepts the local pear ``id,ec`` format and common variants.
    Rows remain source-labelled so aliases never become unqualified sequence
    evidence.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    delimiter = "\t" if "\t" in lines[0] else ","
    rows = csv.DictReader(lines, delimiter=delimiter)
    output = []
    for row in rows:
        reaction_id = _field(row, "id", "reaction_id", "canonical_id", "bigg_id")
        ecs = metabolic_normalize_ec_values(_field(row, "ec", "ec_code", "ec_numbers"))
        if not reaction_id:
            continue
        for ec in ecs:
            output.append({"source_id": reaction_id, "ec": ec})
    return output


def apply_ec_aliases(model, paths: list[Path]) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Annotate matching reactions and return an auditable alias ledger."""
    by_id = {reaction.id: reaction for reaction in model.reactions}
    by_alias = {}
    for reaction in model.reactions:
        for namespace in ("bigg.reaction", "seed.reaction", "modelseed.reaction"):
            aliases = reaction.annotation.get(namespace, [])
            aliases = [aliases] if isinstance(aliases, str) else list(aliases or [])
            for alias in aliases:
                by_alias.setdefault(str(alias), reaction)
    ledger: list[dict[str, str]] = []
    rows_by_canonical: dict[str, list[dict[str, str]]] = defaultdict(list)
    matched = 0
    unmatched = 0
    for path in paths:
        for row in read_ec_aliases(path):
            reaction = by_id.get(row["source_id"]) or by_alias.get(row["source_id"])
            entry = {
                "source": str(path.resolve()),
                "source_id": row["source_id"],
                "canonical_id": reaction.id if reaction else "",
                "ec": row["ec"],
                "status": "matched",
            }
            ledger.append(entry)
            if reaction is None:
                entry["status"] = "unmatched_reaction"
                unmatched += 1
                continue
            if reaction.boundary:
                entry["status"] = "boundary_reaction_excluded"
                unmatched += 1
                continue
            rows_by_canonical[reaction.id].append(entry)
            values = reaction.annotation.get("ec-code", [])
            values = [values] if isinstance(values, str) else list(values or [])
            reaction.annotation["ec-code"] = sorted(set(values + [row["ec"]]))
            matched += 1
    for reaction in model.reactions:
        rows = rows_by_canonical.get(reaction.id, [])
        if rows:
            reaction.notes["ec_alias_sources"] = json.dumps(
                sorted({row["source"] for row in rows}), ensure_ascii=False
            )
    return ledger, {"rows": len(ledger), "matched": matched, "unmatched": unmatched}


__all__ = ["apply_ec_aliases", "read_ec_aliases"]
