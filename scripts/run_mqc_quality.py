#!/usr/bin/env python3
"""Run the user-authorized external MQC package on one SBML model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.metabolic.qc.mqc import run_mqc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--mqc-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--timeout", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_mqc(
        args.model,
        args.output_dir,
        args.mqc_root,
        python_executable=args.python_executable,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
