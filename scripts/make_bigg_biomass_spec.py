#!/usr/bin/env python3
"""Create a biomass-library source spec for all BiGG references in a mapping."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_BOUNDARY_PREFIXES = ("EX", "DM", "SK")
_BOUNDARY_WORDS = {"exchange", "demand", "sink"}
_BIOMASS_WORD_RE = re.compile(r"(?:biomass|growth|cell[\s_-]*mass)", re.I)


def _candidate_id(candidate: object) -> str:
    """Return a stable reaction ID from a registry candidate record."""
    if isinstance(candidate, dict):
        return str(candidate.get("id", "")).strip()
    return str(candidate).strip()


def _candidate_text(candidate: object) -> str:
    """Return searchable ID/name text without opening a source model."""
    if isinstance(candidate, dict):
        return " ".join(
            str(candidate.get(key, ""))
            for key in ("id", "name", "reaction_type", "type")
        ).strip()
    return _candidate_id(candidate)


def _boundary_reason(candidate: object) -> str | None:
    """Classify explicit boundary reaction metadata conservatively."""
    reaction_id = _candidate_id(candidate).upper()
    if not reaction_id:
        return "missing_reaction_id"
    if reaction_id.split("_", 1)[0] in _BOUNDARY_PREFIXES:
        return "boundary_reaction"
    if isinstance(candidate, dict):
        if candidate.get("boundary") is True:
            return "boundary_reaction"
        kind = str(candidate.get("reaction_type", candidate.get("type", ""))).casefold()
        if kind in _BOUNDARY_WORDS:
            return f"{kind}_reaction"
    # Registry records normally contain only IDs.  Reject clearly labelled
    # exchange/demand/sink candidates even when they use a non-standard ID.
    words = set(re.findall(r"[a-z]+", _candidate_text(candidate).casefold()))
    for word in _BOUNDARY_WORDS:
        if word in words:
            return f"{word}_reaction"
    return None


def _is_biomass_like(candidate: object) -> bool:
    """Require an objective name that describes biomass/growth/cell mass."""
    return bool(_BIOMASS_WORD_RE.search(_candidate_text(candidate)))


def select_biomass_reaction(record: dict) -> dict:
    """Select one auditable biomass objective from a registry record.

    BiGG objective extraction can return every reaction carrying a non-zero
    objective coefficient (for example, ``iAF987`` includes many exchanges and
    one real biomass reaction).  The old caller selected ``reactions[0]`` and
    could therefore turn an exchange into the biomass equation.  This helper
    filters boundary/non-biomass candidates and only returns a reaction when
    exactly one eligible ID remains.  The full candidate/exclusion ledger is
    retained for the generated source spec.
    """
    raw_candidates = record.get("biomass_reactions", []) if isinstance(record, dict) else []
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    # Preserve registry order for the audit trail while de-duplicating IDs for
    # uniqueness.  Empty IDs are retained in exclusions, never selected.
    candidates = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        reaction_id = _candidate_id(candidate)
        if reaction_id not in seen:
            candidates.append(candidate)
            seen.add(reaction_id)

    eligible: list[str] = []
    excluded: list[dict[str, object]] = []
    for candidate in candidates:
        reaction_id = _candidate_id(candidate)
        reasons = []
        boundary_reason = _boundary_reason(candidate)
        if boundary_reason:
            reasons.append(boundary_reason)
        if not _is_biomass_like(candidate):
            reasons.append("not_biomass_like")
        if reasons:
            excluded.append({"reaction": reaction_id or None, "reasons": reasons})
        else:
            eligible.append(reaction_id)

    payload: dict[str, object] = {
        "status": "blocked",
        "reaction": None,
        "reason": (
            "missing_objective_reactions"
            if not candidates
            else "no_eligible_biomass_objective"
        ),
        "candidates": [_candidate_id(candidate) for candidate in candidates],
        "eligible": eligible,
        "excluded": excluded,
    }
    registry_status = record.get("biomass_status") if isinstance(record, dict) else None
    if registry_status:
        # Keep the extractor's original status even when semantic filtering
        # recovers one unambiguous biomass-like candidate from an otherwise
        # ambiguous objective set.
        payload["registry_status"] = str(registry_status)
    if len(eligible) == 1:
        payload.update(
            status="selected",
            reaction=eligible[0],
            reason=(
                "unique_biomass_objective"
                if len(candidates) == 1
                else "unique_biomass_objective_after_boundary_and_semantic_filter"
            ),
        )
    elif len(eligible) > 1:
        payload["reason"] = "ambiguous_biomass_objectives"
    return payload


def _load_registry(path: Path) -> tuple[dict[str, str], dict[str, dict]]:
    """Load selected reactions and per-model selection audit records."""
    raw_registry = json.loads(path.read_text(encoding="utf-8"))
    selected: dict[str, str] = {}
    audit: dict[str, dict] = {}
    for item in raw_registry.get("records", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        choice = select_biomass_reaction(item)
        audit[model_id] = choice
        if choice["status"] == "selected":
            selected[model_id] = str(choice["reaction"])
    return selected, audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    registry: dict[str, str] = {}
    registry_audit: dict[str, dict] = {}
    if args.registry:
        registry, registry_audit = _load_registry(args.registry)
    sources = {}
    for build in mapping.get("builds", []):
        for reference in build.get("references", []):
            model_id = reference["model_id"]
            if model_id in sources:
                continue
            compressed = args.model_directory / f"{model_id}.json.gz"
            xml = args.model_directory / f"{model_id}.xml"
            model = compressed if compressed.is_file() else xml
            if not model.is_file():
                continue
            source = {
                "id": model_id,
                "organism": build.get("organism", model_id),
                "kingdom": build.get("kingdom", "bacteria"),
                "gram": "unspecified",
                "model": str(model.resolve()),
                "source_url": reference.get("source_url", ""),
                "license": (
                    "BiGG Models public model metadata; verify upstream license before "
                    "redistribution"
                ),
            }
            if model_id in registry:
                source["biomass_reaction"] = registry[model_id]
            if model_id in registry_audit:
                # Keep the complete candidate/exclusion ledger beside the
                # selected ID, including blocked ambiguous/missing models.
                source["biomass_selection"] = registry_audit[model_id]
            elif args.registry:
                source["biomass_selection"] = {
                    "status": "blocked",
                    "reaction": None,
                    "reason": "registry_record_missing",
                    "candidates": [],
                    "eligible": [],
                    "excluded": [],
                }
            sources[model_id] = source
    blocked = [
        {"model_id": model_id, "selection": source["biomass_selection"]}
        for model_id, source in sources.items()
        if source.get("biomass_selection", {}).get("status") == "blocked"
    ]
    payload = {
        "schema_version": 1,
        "sources": list(sources.values()),
        "biomass_selection_summary": {
            "registry": str(args.registry.resolve()) if args.registry else None,
            "selected": sum("biomass_reaction" in source for source in sources.values()),
            "blocked": blocked,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    missing = len(
        {
            reference["model_id"]
            for build in mapping.get("builds", [])
            for reference in build.get("references", [])
        }
    ) - len(sources)
    print(json.dumps({"sources": len(sources), "missing": missing}))


if __name__ == "__main__":
    main()
