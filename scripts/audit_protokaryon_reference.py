#!/usr/bin/env python3
"""Audit a Protokaryon SBML layer before using it as a reference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.metabolic.qc.protokaryon import audit_reference_model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coefficient-threshold", type=float, default=100.0)
    args = parser.parse_args()
    report = audit_reference_model(
        args.model,
        comparison_path=args.compare,
        coefficient_threshold=args.coefficient_threshold,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"output": str(args.output), "valid": report["valid_as_chemical_reference"]},
            ensure_ascii=False,
        )
    )
    return 0 if report["valid_as_chemical_reference"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
