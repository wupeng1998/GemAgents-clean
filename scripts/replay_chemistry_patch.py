"""Replay and verify an auditable chemistry metadata patch on an SBML snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.metabolic.library.qc import ChemistryChange, apply_chemistry_patch


def replay_patch_file(model_path: Path, patch_path: Path, output_path: Path) -> dict[str, object]:
    from cobra.io import read_sbml_model, write_sbml_model

    payload = json.loads(patch_path.read_text(encoding="utf-8"))
    model = read_sbml_model(str(model_path))
    changes = tuple(ChemistryChange(**row) for row in payload["changes"])
    replayed = apply_chemistry_patch(model, changes)
    if replayed.status != "applied":
        raise RuntimeError(f"Patch replay failed: {replayed.rollback_reason}")
    if payload.get("patch_id") and replayed.patch_id != payload["patch_id"]:
        raise RuntimeError("Patch identity differs from the recorded patch")
    if payload.get("after_static") and replayed.after_static != payload["after_static"]:
        raise RuntimeError("Replayed static checks differ from the recorded checks")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_sbml_model(model, str(output_path))
    return replayed.as_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("patch", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    report = replay_patch_file(arguments.model, arguments.patch, arguments.output)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
