#!/usr/bin/env python3
"""Run fast closed-boundary energy/carrier probes on a COBRA model."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from cobra.io import read_sbml_model

from GemAgents.metabolic.library.quality import (
    apply_canonical_chemistry,
    guard_energy_hydrolysis_direction,
)
from GemAgents.metabolic.qc import Auditor, Probe


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(model_path: Path) -> dict:
    model = read_sbml_model(str(model_path))
    model.solver = "glpk"
    chemistry_corrections = apply_canonical_chemistry(model)
    direction_guards = []
    for reaction in model.reactions:
        if guard_energy_hydrolysis_direction(reaction):
            direction_guards.append(reaction.id)
    baseline = model.optimize()
    specs = (
        ("ATP", "atp_c", "adp_c", {"h2o_c": -1, "pi_c": 1, "h_c": 1}),
        ("GTP", "gtp_c", "gdp_c", {"h2o_c": -1, "pi_c": 1, "h_c": 1}),
        ("CTP", "ctp_c", "cdp_c", {"h2o_c": -1, "pi_c": 1, "h_c": 1}),
        ("UTP", "utp_c", "udp_c", {"h2o_c": -1, "pi_c": 1, "h_c": 1}),
        ("ITP", "itp_c", "idp_c", {"h2o_c": -1, "pi_c": 1, "h_c": 1}),
        ("NADH", "nadh_c", "nad_c", {"h_c": 1}),
        ("NADPH", "nadph_c", "nadp_c", {"h_c": 1}),
        ("FADH2", "fadh2_c", "fad_c", {"h_c": 1}),
        ("FMNH2", "fmnh2_c", "fmn_c", {"h_c": 1}),
        ("Q8H2", "q8h2_c", "q8_c", {"h_c": 1}),
    )
    auditor = Auditor(timeout=30, cache=False)
    results = []
    for name, high, low, cofactors in specs:
        identifiers = (high, low, *cofactors)
        if not all(identifier in model.metabolites for identifier in identifiers):
            results.append({"name": name, "status": "not_applicable"})
            continue
        drain = {high: -1, low: 1, **cofactors}
        results.append(asdict(auditor.audit(model, Probe(name, (drain,)))))
    return {
        "model": str(model_path.resolve()),
        "model_sha256": sha256(model_path),
        "dimensions": {
            "reactions": len(model.reactions),
            "metabolites": len(model.metabolites),
            "genes": len(model.genes),
        },
        "baseline_objective": {
            "variables": [variable.name for variable in model.objective.variables],
            "status": str(baseline.status),
            "value": None if baseline.objective_value is None else float(baseline.objective_value),
        },
        "boundary_closure": "all COBRA boundary reactions closed in private probe copies",
        "canonical_chemistry_corrections": chemistry_corrections,
        "energy_direction_guards": sorted(direction_guards),
        "probes": results,
        "lp_calls": auditor.lp_calls,
        "interpretation": (
            "A nonzero carrier maximum is evidence of a closed-boundary carrier cycle, "
            "not biological production."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(args.output), "lp_calls": report["lp_calls"]}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
