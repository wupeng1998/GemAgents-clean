#!/usr/bin/env python3
"""Build metabolic models for the repository's ``bigg`` protein directory.

This is a small, deterministic entry point around
``run_bigg_gemagents_batch.py``.  With no arguments it uses the repository
layout requested by the project::

    conda run -n gemagents env PYTHONPATH=src \
      python scripts/build_bigg_models.py

The underlying batch runner keeps per-genome manifests, resumes valid cached
builds, and writes progress to ``bigg_model/batch-status.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _workspace_default() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve_path(value: Path, workspace: Path) -> Path:
    """Resolve a CLI path relative to the selected workspace."""
    return (workspace / value if not value.is_absolute() else value).expanduser().resolve()


def _mapping_candidates(output_directory: Path) -> list[Path]:
    preparation = output_directory / "preparation"
    candidates: list[Path] = []
    for pattern in (
        "mapping.repaired_native_*.json",
        "mapping.v2.*.json",
        "mapping.native.json",
        "mapping.json",
        "mapping.explicit.auto.json",
    ):
        candidates.extend(
            sorted(
                preparation.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        )
    return candidates


def _find_mapping(output_directory: Path, requested: Path | None) -> Path:
    from GemAgents.batch_mapping import validate_prepared_mapping

    candidates = [requested] if requested is not None else _mapping_candidates(output_directory)
    invalid: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser().resolve()
        if not candidate.is_file():
            invalid.append(f"missing: {candidate}")
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            validate_prepared_mapping(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            invalid.append(f"invalid: {candidate} ({error})")
            continue
        return candidate
    searched = ", ".join(invalid) if invalid else str(output_directory / "preparation")
    raise SystemExit(
        "No valid batch mapping was found. Searched "
        f"{searched}. Pass --mapping or prepare a mapping under "
        f"{output_directory / 'preparation'}."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build metabolic models for every mapped FAA sequence in bigg."
    )
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--input-directory", type=Path)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument(
        "--mapping", type=Path, help="prepared mapping JSON; auto-detected by default"
    )
    parser.add_argument(
        "--biomass-library",
        type=Path,
    )
    parser.add_argument("--reaction-library", type=Path)
    parser.add_argument("--hmm-directory", type=Path)
    parser.add_argument("--clean-predictions-directory", type=Path)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--max-builds", type=int)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument(
        "--no-resume", action="store_true", help="disable reuse of valid cached builds"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resolved command without running it"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = (args.workspace or _workspace_default()).expanduser().resolve()
    input_directory = _resolve_path(args.input_directory or Path("bigg"), workspace)
    output_directory = _resolve_path(args.output_directory or Path("bigg_model"), workspace)
    if not input_directory.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_directory}")
    requested_mapping = _resolve_path(args.mapping, workspace) if args.mapping else None
    mapping = _find_mapping(output_directory, requested_mapping)

    biomass_library = (
        args.biomass_library or workspace / "data/prokaryotic_biomass_library_v2"
    )
    if args.reaction_library:
        reaction_library = args.reaction_library
    else:
        from GemAgents.contracts import default_reaction_library_path

        reaction_library = workspace / default_reaction_library_path(workspace)
    hmm_directory = args.hmm_directory or workspace / "data/ncbi_hmm"
    biomass_library = _resolve_path(biomass_library, workspace)
    reaction_library = _resolve_path(reaction_library, workspace)
    hmm_directory = _resolve_path(hmm_directory, workspace)
    for label, path in (
        ("biomass library", biomass_library),
        ("reaction library", reaction_library),
        ("HMM directory", hmm_directory),
    ):
        if not path.is_dir():
            raise SystemExit(f"{label} does not exist: {path.resolve()}")

    script = workspace / "scripts/run_bigg_gemagents_batch.py"
    if not script.is_file():
        raise SystemExit(f"Batch runner is missing: {script}")
    command = [
        sys.executable,
        str(script),
        "--workspace",
        str(workspace),
        "--mapping",
        str(mapping),
        "--biomass-library",
        str(biomass_library.resolve()),
        "--reaction-library",
        str(reaction_library.resolve()),
        "--hmm-directory",
        str(hmm_directory.resolve()),
        "--output-directory",
        str(output_directory),
        "--cpus",
        str(args.cpus),
    ]
    if args.clean_predictions_directory is not None:
        command.extend(
            [
                "--clean-predictions-directory",
                str(args.clean_predictions_directory.expanduser().resolve()),
            ]
        )
    if args.max_builds is not None:
        command.extend(["--max-builds", str(args.max_builds)])
    for build_id in args.only:
        command.extend(["--only", build_id])
    if args.no_resume:
        command.append("--no-resume")

    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "input_directory": str(input_directory),
                "mapping": str(mapping),
                "command": command,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.dry_run:
        return 0

    environment = os.environ.copy()
    source_root = str(workspace / "src")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + environment.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)
    completed = subprocess.run(command, cwd=workspace, env=environment, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
