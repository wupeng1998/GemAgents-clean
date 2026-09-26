"""Apply the audited ATPM direction correction to the strict library view.

The complete union remains byte-for-byte source evidence.  Only the strict
operational view and its QC table are changed; native full-union loading also
applies the same code-level guard.
"""

from __future__ import annotations

import csv
import gzip
import json
import tempfile
from pathlib import Path

from cobra.io import read_sbml_model, write_sbml_model

from GemAgents.metabolic.io import metabolic_hash
from GemAgents.metabolic.library.quality import guard_energy_hydrolysis_direction

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "data/reaction_library_public_union_annotated_20260922"


def main() -> None:
    manifest_path = LIB / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    strict_path = LIB / "universe.xml.gz"
    with (
        gzip.open(strict_path, "rb") as source,
        tempfile.NamedTemporaryFile(suffix=".xml") as target,
    ):
        target.write(source.read())
        target.flush()
        model = read_sbml_model(target.name)
    reaction = model.reactions.get_by_id("ATPM")
    before = list(reaction.bounds)
    changed = guard_energy_hydrolysis_direction(reaction)
    after = list(reaction.bounds)
    if before == [0.0, 999999.0]:
        changed = False
    elif not changed or before != [-1000.0, 999999.0] or after[0] != 0.0:
        raise RuntimeError(
            f"unexpected ATPM correction: before={before}, after={after}, changed={changed}"
        )
    ppk = model.reactions.get_by_id("PPK")
    ppk_before = list(ppk.bounds)
    ppk_changed = guard_energy_hydrolysis_direction(ppk)
    if ppk_before == [0.0, 1000000.0]:
        ppk_changed = False
    elif not ppk_changed or ppk_before != [-1000000.0, 1000000.0] or ppk.lower_bound != 0.0:
        raise RuntimeError(
            "unexpected PPK correction: "
            f"before={ppk_before}, after={ppk.bounds}, changed={ppk_changed}"
        )
    changed = changed or ppk_changed
    extra_bounds = {}
    for rid, expected_before in (("PPA", [-1000.0, 1000000.0]), ("PTPATi", [-999999.0, 1000000.0])):
        candidate = model.reactions.get_by_id(rid)
        candidate_before = list(candidate.bounds)
        candidate_changed = guard_energy_hydrolysis_direction(candidate)
        if candidate_before == [0.0, expected_before[1]]:
            candidate_changed = False
        elif (
            not candidate_changed
            or candidate_before != expected_before
            or candidate.lower_bound != 0.0
        ):
            raise RuntimeError(
                f"unexpected {rid} correction: "
                f"before={candidate_before}, after={candidate.bounds}, "
                f"changed={candidate_changed}"
            )
        changed = changed or candidate_changed
        extra_bounds[rid] = {"before": candidate_before, "after": list(candidate.bounds)}
    write_sbml_model(model, str(strict_path))

    quality_path = LIB / "reaction_quality.tsv"
    with quality_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if changed:
        bounds = {
            "ATPM": after,
            "PPK": list(ppk.bounds),
            **{rid: value["after"] for rid, value in extra_bounds.items()},
        }
        for row in rows:
            if row["reaction_id"] in bounds:
                row["direction"] = "forward"
                row["lower_bound"] = str(bounds[row["reaction_id"]][0])
                row["upper_bound"] = str(bounds[row["reaction_id"]][1])
        with quality_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    reactions_path = LIB / "reactions.tsv"
    with reactions_path.open(encoding="utf-8", newline="") as handle:
        reaction_rows = list(csv.DictReader(handle, delimiter="\t"))
    bounds = {
        "ATPM": after,
        "PPK": list(ppk.bounds),
        **{rid: value["after"] for rid, value in extra_bounds.items()},
    }
    for row in reaction_rows:
        if row["id"] in bounds:
            row["lower_bound"] = str(bounds[row["id"]][0])
            row["upper_bound"] = str(bounds[row["id"]][1])
    with reactions_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=reaction_rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(reaction_rows)

    quality_json_path = LIB / "reaction_quality.json"
    quality_json = json.loads(quality_json_path.read_text())
    direction_counts = {}
    for row in rows:
        direction_counts[f"direction_{row['direction']}"] = (
            direction_counts.get(f"direction_{row['direction']}", 0) + 1
        )
    quality_json["counts"].update(direction_counts)
    quality_json["energy_direction_guards"] = ["ATPM", "PPK", "PPA", "PTPATi"]
    quality_json_path.write_text(json.dumps(quality_json, indent=2) + "\n")

    feedback = {
        "status": "applied",
        "scope": "strict operational library only",
        "reaction_id": ["ATPM", "PPK", "PPA", "PTPATi"],
        "reason": (
            "Public direction union admitted reverse ATP-generating task reactions; "
            "published iML1515 uses forward-only directions."
        ),
        "before_bounds": {
            "ATPM": [-1000.0, 999999.0],
            "PPK": [-1000000.0, 1000000.0],
            "PPA": [-1000.0, 1000000.0],
            "PTPATi": [-999999.0, 1000000.0],
        },
        "after_bounds": {
            "ATPM": [0.0, 999999.0],
            "PPK": [0.0, 1000000.0],
            "PPA": [0.0, 1000000.0],
            "PTPATi": [0.0, 1000000.0],
        },
        "evidence": {
            "reconstructed_growth": 1.288178444139485,
            "published_iml1515_growth": 0.8769972144269684,
            "reconstructed_atpm_flux": -99.25305068342284,
            "reconstructed_ppk_flux": -93.166383953325,
            "reconstructed_v8_growth": 1.23962595774276,
            "reconstructed_v8_ppa_flux": -49.284306395290145,
            "reconstructed_v8_ptpati_flux": -50.5958244604522,
            "unbalanced_reactions_in_reconstructed_model": 12,
            "closed_boundary_material_generation": 0,
            "energy_reverse_atpm_cycle_without_external_supply": 0,
        },
        "source_evidence_preserved": [
            "universe_full.xml.gz",
            "reaction_catalog.jsonl",
            "reaction_evidence.jsonl",
            "bigg_direction_union.json",
        ],
    }
    (LIB / "qc_feedback.json").write_text(json.dumps(feedback, indent=2) + "\n")
    manifest["qc_feedback"] = {
        "status": "applied",
        "file": "qc_feedback.json",
        "strict_view_changes": [
            "ATPM lower_bound -1000.0 -> 0.0",
            "ATPM direction reversible -> forward",
            "PPK lower_bound -1000000.0 -> 0.0",
            "PPK direction reversible -> forward",
            "PPA lower_bound -1000.0 -> 0.0",
            "PPA direction reversible -> forward",
            "PTPATi lower_bound -999999.0 -> 0.0",
            "PTPATi direction reversible -> forward",
        ],
        "full_union_unchanged": True,
    }
    manifest["quality_control"]["counts"]["direction_forward"] = direction_counts[
        "direction_forward"
    ]
    manifest["quality_control"]["counts"]["direction_reversible"] = direction_counts[
        "direction_reversible"
    ]
    manifest["model_sha256"] = metabolic_hash(strict_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "changed": changed,
                "before": before,
                "after": after,
                "model_sha256": manifest["model_sha256"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
