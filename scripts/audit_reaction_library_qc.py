#!/usr/bin/env python3
"""Create a reproducible per-reaction QC audit for a reaction library.

The library builder already evaluates every source reaction.  This command
turns that row-level result into a compact, independently re-counted report
that is easy to review alongside MQC output.  It deliberately does not alter
the catalog or either SBML view.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(library: Path) -> dict:
    manifest_path = library / "manifest.json"
    quality_path = library / "reaction_quality.tsv"
    if not manifest_path.is_file() or not quality_path.is_file():
        raise FileNotFoundError("library requires manifest.json and reaction_quality.tsv")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    statuses = Counter()
    directions = Counter()
    chemistry = Counter()
    active = Counter()
    by_status: defaultdict[str, list[str]] = defaultdict(list)
    rows = 0
    with quality_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            rows += 1
            status = row["status"]
            statuses[status] += 1
            directions[row["direction"]] += 1
            chemistry[row["chemistry_class"]] += 1
            active[str(row["active"]).casefold()] += 1
            by_status[status].append(row["reaction_id"])

    expected = manifest.get("quality_control", {})
    expected_source = int(expected.get("source_reactions", rows))
    expected_active = int(expected.get("strict_active_reactions", 0))
    expected_rejected = int(expected.get("rejected_reactions", 0))
    manifest_directions = {
        key.removeprefix("direction_"): int(value)
        for key, value in expected.get("counts", {}).items()
        if key.startswith("direction_")
    }
    report = {
        "schema_version": 1,
        "library": str(library.resolve()),
        "manifest_sha256": sha256(manifest_path),
        "quality_table": {
            "path": str(quality_path.resolve()),
            "sha256": sha256(quality_path),
            "rows": rows,
        },
        "source": {
            "manifest_status": manifest.get("status"),
            "source_reactions": expected_source,
            "full_union_reactions": manifest.get("counts", {}).get("full_union_reactions"),
            "full_union_metabolites": manifest.get("counts", {}).get("full_union_metabolites"),
            "strict_active_reactions": expected_active,
            "rejected_reactions": expected_rejected,
        },
        "protokaryon_reference": {
            "ec_aliases": manifest.get("ec_aliases")
            or manifest.get("sources", {}).get("ec_aliases"),
            "build_recipe": manifest.get("build_recipe", {}).get("parameters", {}),
        },
        "recount": {
            "rows": rows,
            "status_counts": dict(sorted(statuses.items())),
            "direction_counts": dict(sorted(directions.items())),
            "chemistry_class_counts": dict(sorted(chemistry.items())),
            "active_counts": dict(sorted(active.items())),
            "manifest_direction_counts": manifest_directions,
            "direction_counts_match_manifest": dict(sorted(directions.items()))
            == dict(sorted(manifest_directions.items())),
            "rejected_ids_by_status": {
                key: sorted(value)
                for key, value in sorted(by_status.items())
                if key not in {"pass", "boundary_or_pseudoreaction_allowed"}
            },
        },
        "consistency": {
            "row_count_matches_manifest": rows == expected_source,
            "strict_active_matches_manifest": active.get("true", 0) == expected_active,
            "rejected_matches_manifest": sum(
                count
                for status, count in statuses.items()
                if status not in {"pass", "boundary_or_pseudoreaction_allowed"}
            ) == expected_rejected,
            "direction_counts_match_manifest": dict(sorted(directions.items()))
            == dict(sorted(manifest_directions.items())),
            "quality_complete": bool(expected.get("quality_complete")),
        },
        "energy_direction_guards": manifest.get("energy_direction_guards", []),
        "qc_feedback": manifest.get("qc_feedback", {}),
        "policy": expected.get("policy"),
    }
    # Direction counts are reported separately because reference-direction
    # reconciliation can update serialized bounds after the original manifest
    # was written.  The row-level chemistry/active-set checks remain the gate.
    report["consistency"]["all_checks_pass"] = all(
        report["consistency"][key]
        for key in (
            "row_count_matches_manifest",
            "strict_active_matches_manifest",
            "rejected_matches_manifest",
            "direction_counts_match_manifest",
            "quality_complete",
        )
    )
    return report


def independent_model_recheck(model_path: Path) -> dict:
    """Re-run the library QC implementation on the serialized full union."""
    from cobra.io import read_sbml_model

    from GemAgents.metabolic.library.quality import metabolic_quality_control_library

    model = read_sbml_model(str(model_path))
    _, _, quality = metabolic_quality_control_library(model)
    return {
        "model": str(model_path.resolve()),
        "model_sha256": sha256(model_path),
        "reactions": len(model.reactions),
        "metabolites": len(model.metabolites),
        "quality_report": quality,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model",
        type=Path,
        help="optional serialized full-union SBML for an independent recheck",
    )
    args = parser.parse_args()
    report = audit(args.library)
    if args.model:
        report["independent_model_recheck"] = independent_model_recheck(args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "all_checks_pass": report["consistency"]["all_checks_pass"],
                "rows": report["recount"]["rows"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["consistency"]["all_checks_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
