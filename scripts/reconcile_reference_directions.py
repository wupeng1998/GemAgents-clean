"""Reconcile GemAgents reaction direction classes with a reference model.

Only direction classes are imported: reaction equations, annotations, and
stoichiometric magnitudes stay untouched.  The original GemAgents views are
copied before an apply, and the reference model hash plus every decision are stored
in the library manifest/report.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from cobra.io import read_sbml_model, write_sbml_model

from GemAgents.metabolic.io import metabolic_hash
from GemAgents.metabolic.library.normalization import metabolic_equation_key

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "data/reaction_library_public_union_annotated_20260922"
REFERENCE_ENV = "GEMAGENTS_DIRECTION_REFERENCE_MODEL"
EPS = 1e-9


def _load_gz_model(path: Path):
    with gzip.open(path, "rb") as source, tempfile.NamedTemporaryFile(suffix=".xml") as target:
        target.write(source.read())
        target.flush()
        return read_sbml_model(target.name)


def _fingerprint(reaction):
    return metabolic_equation_key(
        {metabolite.id: float(value) for metabolite, value in reaction.metabolites.items()}
    )


def _direction(reaction):
    lower, upper = reaction.bounds
    if abs(lower) <= EPS and abs(upper) <= EPS:
        return "blocked"
    if lower < -EPS and upper > EPS:
        return "reversible"
    if upper > EPS and lower >= -EPS:
        return "forward"
    if lower < -EPS and upper <= EPS:
        return "reverse"
    return "blocked"


def _mapped_direction(reference_reaction, gem_reaction):
    reference_key, reference_pivot = _fingerprint(reference_reaction)
    gem_key, gem_pivot = _fingerprint(gem_reaction)
    if reference_key != gem_key:
        return None
    direction = _direction(reference_reaction)
    # A negative equation scale means the reference forward flux is GemAgents'
    # reverse flux.  Swap the direction class in that case.
    if float(reference_pivot / gem_pivot) < 0:
        direction = {
            "forward": "reverse",
            "reverse": "forward",
            "reversible": "reversible",
            "blocked": "blocked",
        }[direction]
    return direction


def _apply_direction(gem_reaction, direction, reference_bounds):
    lower, upper = gem_reaction.bounds
    capacity = max(
        abs(float(lower)),
        abs(float(upper)),
        abs(float(reference_bounds[0])),
        abs(float(reference_bounds[1])),
    )
    if direction == "blocked":
        new_bounds = (0.0, 0.0)
    elif direction == "forward":
        new_bounds = (0.0, capacity)
    elif direction == "reverse":
        new_bounds = (-capacity, 0.0)
    else:
        new_bounds = (-capacity, capacity)
    if new_bounds == gem_reaction.bounds:
        return False
    gem_reaction.bounds = new_bounds
    gem_reaction.notes["reference_direction_reconciliation"] = direction
    return True


def _match(gem, reference):
    by_id = {reaction.id: reaction for reaction in reference.reactions}
    by_equation = defaultdict(list)
    for reaction in reference.reactions:
        try:
            by_equation[_fingerprint(reaction)[0]].append(reaction)
        except (TypeError, ValueError):
            continue
    decisions = []
    for reaction in gem.reactions:
        candidates = []
        source = by_id.get(reaction.id)
        if source is not None and _mapped_direction(source, reaction) is not None:
            candidates = [source]
            match_type = "same_id_equation"
        else:
            try:
                candidates = by_equation.get(_fingerprint(reaction)[0], [])
            except (TypeError, ValueError):
                candidates = []
            match_type = "unique_equation" if len(candidates) == 1 else "ambiguous_equation"
        if len(candidates) != 1:
            decisions.append(
                {
                    "reaction_id": reaction.id,
                    "status": "unmatched" if not candidates else "ambiguous",
                    "gem_direction": _direction(reaction),
                    "candidate_count": len(candidates),
                }
            )
            continue
        source = candidates[0]
        direction = _mapped_direction(source, reaction)
        decisions.append(
            {
                "reaction_id": reaction.id,
                "reference_reaction_id": source.id,
                "match_type": match_type,
                "status": "matched",
                "gem_direction": _direction(reaction),
                "reference_direction": direction,
                "reference_bounds": list(source.bounds),
                "gem_bounds_before": list(reaction.bounds),
                "equation": reaction.reaction,
            }
        )
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reset-prereference", action="store_true")
    parser.add_argument(
        "--reference-model",
        default=os.environ.get(REFERENCE_ENV),
        help=f"reference SBML path (or {REFERENCE_ENV})",
    )
    args = parser.parse_args()
    if not args.reference_model:
        raise SystemExit(f"--reference-model or {REFERENCE_ENV} is required")
    reference_path = Path(args.reference_model).expanduser().resolve()
    if not reference_path.is_file():
        raise SystemExit(f"reference direction model not found: {reference_path}")
    report_path = LIB / "reference_direction_reconciliation.json"
    if args.apply and report_path.exists():
        existing = json.loads(report_path.read_text())
        if existing.get("status") == "applied" and not args.force:
            raise SystemExit("reference direction reconciliation is already applied")
    if args.reset_prereference:
        for filename in (
            "universe_full.xml.gz",
            "universe.xml.gz",
            "universe_bigg_full.xml.gz",
            "universe_bigg.xml.gz",
        ):
            backup = LIB / f"{filename.removesuffix('.xml.gz')}_prereference.xml.gz"
            if not backup.is_file():
                raise SystemExit(f"missing pre-reference backup: {backup}")
            shutil.copyfile(backup, LIB / filename)
    reference = read_sbml_model(str(reference_path))
    reference_hash = hashlib.sha256(reference_path.read_bytes()).hexdigest()
    decisions_by_view = {}
    model_paths = {
        "full": LIB / "universe_full.xml.gz",
        "strict": LIB / "universe.xml.gz",
        "bigg_full": LIB / "universe_bigg_full.xml.gz",
        "bigg": LIB / "universe_bigg.xml.gz",
    }
    models = {view: (path, _load_gz_model(path)) for view, path in model_paths.items()}
    for view, (_, model) in models.items():
        decisions = _match(model, reference)
        matched = [row for row in decisions if row["status"] == "matched"]
        changes = [row for row in matched if row["gem_direction"] != row["reference_direction"]]
        decisions_by_view[view] = {
            "reaction_count": len(model.reactions),
            "matched": len(matched),
            "unmatched": sum(row["status"] == "unmatched" for row in decisions),
            "ambiguous": sum(row["status"] == "ambiguous" for row in decisions),
            "direction_changes": len(changes),
            "before_direction_counts": dict(Counter(row["gem_direction"] for row in decisions)),
            "reference_direction_counts": dict(
                Counter(row["reference_direction"] for row in matched)
            ),
            "decisions": decisions,
            "direction_changes_sample": [row for row in changes[:200]],
        }
    report = {
        "status": "planned" if not args.apply else "applied",
        "policy": (
            "match same equation; import reference direction class; preserve GemAgents "
            "flux capacity, using reference capacity for previously blocked matches"
        ),
        "reference_source": "configured external SBML",
        "reference_source_sha256": reference_hash,
        "views": {
            view: {key: value for key, value in payload.items() if key != "decisions"}
            for view, payload in decisions_by_view.items()
        },
    }
    if args.apply:
        manifest = json.loads((LIB / "manifest.json").read_text())
        for key in list(manifest):
            if (
                key.endswith("_direction_reconciliation")
                and key != "reference_direction_reconciliation"
            ):
                manifest.pop(key)
        for view, (path, model) in models.items():
            backup = LIB / f"{path.name.removesuffix('.xml.gz')}_prereference.xml.gz"
            if not backup.exists():
                shutil.copyfile(path, backup)
            for row in decisions_by_view[view]["decisions"]:
                if (
                    row["status"] == "matched"
                    and row["gem_direction"] != row["reference_direction"]
                ):
                    _apply_direction(
                        model.reactions.get_by_id(row["reaction_id"]),
                        row["reference_direction"],
                        row["reference_bounds"],
                    )
            write_sbml_model(model, str(path))
            report["views"][view]["sha256"] = metabolic_hash(path)
            report["views"][view]["backup"] = str(backup.name)
            report["views"][view]["backup_sha256"] = metabolic_hash(backup)
        manifest["reference_direction_reconciliation"] = {
            "status": "applied",
            "report": report_path.name,
            "reference_source": "configured external SBML",
            "reference_source_sha256": reference_hash,
            "policy": report["policy"],
            "raw_prereference_backups": [
                "universe_full_prereference.xml.gz",
                "universe_prereference.xml.gz",
                "universe_bigg_full_prereference.xml.gz",
                "universe_bigg_prereference.xml.gz",
            ],
        }
        manifest["full_union_sha256"] = report["views"]["full"]["sha256"]
        manifest["model_sha256"] = report["views"]["strict"]["sha256"]
        manifest["bigg_full_model_sha256"] = report["views"]["bigg_full"]["sha256"]
        manifest["bigg_model_sha256"] = report["views"]["bigg"]["sha256"]
        quality_path = LIB / "reaction_quality.tsv"
        with quality_path.open(encoding="utf-8", newline="") as handle:
            quality_rows = list(csv.DictReader(handle, delimiter="\t"))
        strict_model = models["strict"][1]
        strict_by_id = {reaction.id: reaction for reaction in strict_model.reactions}
        for row in quality_rows:
            reaction = strict_by_id.get(row["reaction_id"])
            if reaction is None:
                continue
            row["direction"] = _direction(reaction)
            row["lower_bound"] = str(float(reaction.lower_bound))
            row["upper_bound"] = str(float(reaction.upper_bound))
        with quality_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=quality_rows[0].keys(), delimiter="\t")
            writer.writeheader()
            writer.writerows(quality_rows)
        quality_json_path = LIB / "reaction_quality.json"
        quality_json = json.loads(quality_json_path.read_text())
        for key in list(quality_json):
            is_reconciliation = key.endswith("_direction_reconciliation")
            if is_reconciliation and key != "reference_direction_reconciliation":
                quality_json.pop(key)
        quality_json["counts"].update(
            {
                f"direction_{direction}": sum(row["direction"] == direction for row in quality_rows)
                for direction in ("forward", "reversible", "reverse", "blocked")
            }
        )
        quality_json["reference_direction_reconciliation"] = report["views"]["strict"]
        quality_json_path.write_text(json.dumps(quality_json, indent=2) + "\n")
        changes_path = LIB / "reference_direction_changes.tsv"
        fields = [
            "view",
            "reaction_id",
            "reference_reaction_id",
            "gem_direction",
            "reference_direction",
            "gem_bounds_before",
            "reference_bounds",
            "equation",
        ]
        with changes_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
            writer.writeheader()
            for view, payload in decisions_by_view.items():
                for row in payload["decisions"]:
                    if (
                        row["status"] != "matched"
                        or row["gem_direction"] == row["reference_direction"]
                    ):
                        continue
                    output_row = {field: row.get(field, "") for field in ("view", *fields[1:])}
                    writer.writerow(output_row | {"view": view})
        report["changes_tsv"] = changes_path.name
        manifest["quality_control"]["counts"].update(
            {
                f"direction_{direction}": sum(row["direction"] == direction for row in quality_rows)
                for direction in ("forward", "reversible", "reverse", "blocked")
            }
        )
        manifest["reference_direction_reconciliation"]["raw_prereference_backups"] = [
            path.name.removesuffix(".xml.gz") + "_prereference.xml.gz"
            for path in model_paths.values()
        ]
        (LIB / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["views"], indent=2))


if __name__ == "__main__":
    main()
