#!/usr/bin/env python3
"""Prepare curated prokaryotic biomass equations and DIAMOND references."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

_REFERENCE_MAP = (
    ("Klebsiella pneumoniae subsp", "Klebsiella pneumoniae subsp.csv"),
    ("ecoli_mg1655", "ecoli_mg1655.csv"),
    (
        "Bacillus subtilis subsp. subtilis str. 168",
        "Bacillus subtilis subsp.csv",
    ),
    ("glutamicum", "glutamicum.csv"),
)
_FALLBACK_ID = "tongyong"
_FALLBACK_FILE = "tongyong.csv"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordered_dict(text: str) -> dict[str, float]:
    if not text.startswith("OrderedDict(") or not text.endswith(")"):
        raise ValueError("Biomass stoichiometry is not an OrderedDict")
    value = ast.literal_eval(text[len("OrderedDict(") : -1])
    pairs = value.items() if isinstance(value, dict) else value
    return {str(key): float(coefficient) for key, coefficient in pairs}


def _compartment(metabolite_id: str) -> str:
    suffix = metabolite_id.rsplit("_", 1)[-1]
    return suffix if suffix in {"c", "e", "p"} else "c"


def _template(template_id: str, csv_path: Path, relative_csv: str) -> dict:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.reader(handle))
    if len(row) < 5:
        raise ValueError(f"Malformed biomass row: {csv_path}")
    stoichiometry = _ordered_dict(row[2])
    source_stoichiometry = {
        metabolite_id: {
            "coefficient": coefficient,
            "compartment": _compartment(metabolite_id),
            "formula": None,
            "charge": None,
            "annotation": {},
        }
        for metabolite_id, coefficient in stoichiometry.items()
    }
    return {
        "id": template_id,
        "source_model_id": template_id,
        "organism": template_id,
        "kingdom": "bacteria",
        "applicable_kingdoms": (
            ["bacteria", "archaea"] if template_id == _FALLBACK_ID else ["bacteria"]
        ),
        "gram": "unspecified",
        "source": "GemAgents prokaryotic biomass library",
        "source_csv": relative_csv,
        "source_csv_sha256": _sha256(csv_path),
        "reference_id": template_id,
        "biomass_source_id": row[0],
        "biomass_reaction": {
            "id": row[0],
            "name": row[1],
            "stoichiometry": stoichiometry,
            "source_id": row[0],
            "source_stoichiometry": source_stoichiometry,
            "source_to_canonical": {
                metabolite_id: metabolite_id for metabolite_id in stoichiometry
            },
            "bounds": [float(row[3]), float(row[4])],
        },
        "gpr_templates": [],
        "support_reactions": [],
        "support_aliases": [],
        "medium_support_reactions": [],
        "selection_aliases": [template_id, row[0]],
        "selection_eligible": True,
        "mapping_status": "complete",
        "usable": True,
        "unmapped_biomass_metabolites": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source_dir = args.source_root / "files" / "biomass" / "Protokaryon"
    database_dir = args.source_root / "homologous_db" / "Protokaryon"
    output = args.output.resolve()
    raw_dir = output / "sources"
    copied_db_dir = output / "homologous_db"
    binary_dir = output / "bin"
    raw_dir.mkdir(parents=True, exist_ok=True)
    copied_db_dir.mkdir(parents=True, exist_ok=True)
    binary_dir.mkdir(parents=True, exist_ok=True)

    source_files = []
    for source in sorted(source_dir.glob("*.csv")):
        target = raw_dir / source.name
        shutil.copy2(source, target)
        source_files.append(
            {"path": f"sources/{source.name}", "sha256": _sha256(target)}
        )

    templates = []
    references = []
    database_files = []
    for template_id, filename in _REFERENCE_MAP:
        csv_path = raw_dir / filename
        templates.append(_template(template_id, csv_path, f"sources/{filename}"))
        source_db = database_dir / f"{template_id}.dmnd"
        target_db = copied_db_dir / source_db.name
        shutil.copy2(source_db, target_db)
        references.append(
            {
                "template": template_id,
                "diamond_database": f"homologous_db/{source_db.name}",
            }
        )
        database_files.append(
            {"path": f"homologous_db/{source_db.name}", "sha256": _sha256(target_db)}
        )

    templates.append(
        _template(_FALLBACK_ID, raw_dir / _FALLBACK_FILE, f"sources/{_FALLBACK_FILE}")
    )
    diamond = binary_dir / "diamond"
    shutil.copy2(args.source_root / "diamond", diamond)
    # The source snapshot carries a debug build larger than common Git
    # hosting limits.  Removing debug symbols preserves program behavior while
    # keeping the self-contained runtime asset reasonably sized.
    subprocess.run(["strip", "--strip-unneeded", str(diamond)], check=True)
    diamond_version = subprocess.run(
        [str(diamond), "version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    data_license = output / "LICENSE.DATA"
    shutil.copy2(args.source_root / "params" / "LICENSE", data_license)
    catalog = {
        "schema_version": 1,
        "selection_method": "diamond_hit_count",
        "source": "GemAgents biomass assets",
        "scope": "prokaryotic biomass equations and deterministic selection",
        "taxonomy_policy": "shared_prokaryotic_biomass",
        "diamond_selection": {
            "identity_threshold": 90.0,
            "e_value_threshold": 1e-10,
            "fallback_template": _FALLBACK_ID,
            "diamond_executable": "bin/diamond",
            "references": references,
        },
        "templates": templates,
        "references": {
            template["reference_id"]: {"id": template["reference_id"], "proteins": []}
            for template in templates
        },
    }
    manifest = {
        "schema_version": 1,
        "selection_method": catalog["selection_method"],
        "taxonomy_policy": catalog["taxonomy_policy"],
        "identity_threshold": 90.0,
        "e_value_threshold": 1e-10,
        "templates": [template["id"] for template in templates],
        "source_files": source_files,
        "database_files": database_files,
        "diamond_executable": {
            "path": "bin/diamond",
            "sha256": _sha256(diamond),
            "version": diamond_version,
            "platform": "linux-x86_64",
        },
        "license": {"path": data_license.name, "sha256": _sha256(data_license)},
    }
    for name, payload in (("catalog.json", catalog), ("manifest.json", manifest)):
        (output / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
