#!/usr/bin/env python3
"""Resolve a historical artifact locator without mutating the repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.relocation import resolve_artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("locator")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--index", type=Path)
    args = parser.parse_args()
    result = resolve_artifact(args.root, args.locator, index_path=args.index)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"PRESENT", "RESOLVED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
