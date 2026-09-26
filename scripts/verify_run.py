"""Independently verify an exported GemAgents SBML model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.metabolic.qc import NumericalPolicy, default_quality_suite, verify_export


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--solver", default="unknown")
    arguments = parser.parse_args()
    certificate = verify_export(
        arguments.model,
        default_quality_suite(),
        NumericalPolicy(solver=arguments.solver),
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(certificate.as_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0 if certificate.required_checks_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
