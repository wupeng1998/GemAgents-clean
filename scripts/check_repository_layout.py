#!/usr/bin/env python3
"""Check for accidental temporary or private files at repository root.

The guard is intentionally narrow: it does not infer that a model, database,
history directory, or JSON artifact is disposable.  Migration candidates are
reported as informational entries and remain untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUFFIXES = frozenset({".bak", ".log", ".pid", ".sock", ".sqlite", ".tmp"})
FORBIDDEN_NAMES = frozenset({".env", "credentials.json", "secrets.json"})
ALLOWED_ROOT_FILES = frozenset(
    {
        ".env.example",
        "AGENT.md",
        "AGENTS.md",
        "README.md",
        "README.zh-CN.md",
        "environment.yml",
        "pyproject.toml",
        "release-manifest.json",
        "research-state.yaml",
        "research-log.md",
        "findings.md",
        "uv.lock",
    }
)


def scan(root: Path) -> dict[str, object]:
    root = root.resolve()
    errors: list[str] = []
    informational: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name in {
            ".git",
            ".agents",
            ".codex",
            ".gemagents",
            ".pytest_cache",
            ".ruff_cache",
        }:
            continue
        if entry.is_file():
            if entry.name in FORBIDDEN_NAMES or entry.suffix.lower() in FORBIDDEN_SUFFIXES:
                errors.append(entry.name)
            elif (
                entry.name not in ALLOWED_ROOT_FILES
                and entry.suffix.lower() in {".json", ".yaml", ".yml"}
            ):
                informational.append(f"review_root_config:{entry.name}")
        elif entry.is_dir() and entry.name in {"bigg", "carveme", "literature", "reconstructor"}:
            informational.append(f"migration_candidate:{entry.name}")
    return {
        "root": str(root),
        "errors": errors,
        "informational": informational,
        "passed": not errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = scan(args.root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"layout guard: {'PASS' if result['passed'] else 'FAIL'}")
        for item in result["informational"]:
            print(f"info: {item}")
        for item in result["errors"]:
            print(f"error: {item}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
