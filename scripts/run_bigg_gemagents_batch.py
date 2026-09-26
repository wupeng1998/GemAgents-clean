#!/usr/bin/env python3
"""Run and resume reference-assisted GemAgents reconstructions for the BiGG benchmark."""

from __future__ import annotations

import argparse
import json
import time
import traceback
import uuid
from pathlib import Path

from GemAgents.batch_mapping import validate_prepared_mapping
from GemAgents.contracts import apply_reconstruction_defaults
from GemAgents.run_cache import (
    SCHEMA_VERSION,
    atomic_json,
    content_identity,
    execution_lock,
    recover_unsealed_manifest,
    runtime_identity,
    seal_manifest,
    stable_key,
    validate_cache,
)
from GemAgents.tools import metabolic_pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=(
            "repository root passed to the metabolic pipeline "
            "(defaults to this script's repository)"
        ),
    )
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--biomass-library", type=Path, required=True)
    parser.add_argument("--reaction-library", type=Path, required=True)
    parser.add_argument("--hmm-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--clean-predictions",
        type=Path,
        help="CLEAN table produced from the selected genome's clean-input-no-ncbi.faa",
    )
    parser.add_argument(
        "--clean-predictions-directory",
        type=Path,
        help="Directory containing per-genome CLEAN tables named by assembly accession",
    )
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--max-builds", type=int)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--no-resume", action="store_false", dest="resume", default=True)
    return parser.parse_args(argv)


def clean_predictions_for_build(args: argparse.Namespace, build: dict) -> Path | None:
    """Resolve a CLEAN table without falling back to another genome's output."""
    clean_path = getattr(args, "clean_predictions", None)
    clean_directory = getattr(args, "clean_predictions_directory", None)
    if clean_path is not None and clean_directory is not None:
        raise ValueError("choose --clean-predictions or --clean-predictions-directory, not both")
    if clean_path is not None:
        return clean_path.resolve()
    directory = clean_directory
    if directory is None:
        return None
    accession = str(build.get("assembly_accession") or build.get("id") or "")
    candidates = []
    for suffix in (".csv", ".tsv", ".txt"):
        candidates.extend(
            (directory / f"{accession}{suffix}", directory / f"{build['id']}{suffix}")
        )
    candidates.extend(
        path
        for prefix in (accession, str(build.get("id") or ""))
        for path in directory.glob(f"{prefix}*")
        if path.suffix.casefold() in {".csv", ".tsv", ".txt"}
    )
    matches = sorted({path.resolve() for path in candidates if path.is_file()})
    if len(matches) > 1:
        raise ValueError(f"multiple CLEAN tables match {build['id']}: {matches}")
    return matches[0] if matches else None


def workspace_from_args(args: argparse.Namespace) -> Path:
    """Return the explicit repository root used by the batch pipeline.

    The fallback keeps compatibility with tests and callers that construct a
    namespace directly while avoiding the process cwd as the normal default.
    """
    value = getattr(args, "workspace", None)
    return (Path(value) if value is not None else Path(__file__).resolve().parents[1]).resolve()


def file_identity(path: str | Path) -> dict:
    return content_identity(path)


def run_identity(
    config: dict,
    *,
    build: dict,
    script: Path,
    common_assets: dict | None = None,
) -> dict:
    repository = Path(__file__).resolve().parents[1]
    assets = {
        "input": file_identity(config["input"]),
        "reference": file_identity(config["reference_support_path"]),
        **(
            common_assets
            if common_assets is not None
            else {
                "reaction_library": file_identity(config["reaction_library"]),
                "biomass_library": file_identity(config["biomass_library"]),
                "hmm_directory": file_identity(config["hmm_dir"]),
                "source_policy": file_identity(repository / "data/source-policy.yaml"),
            }
        ),
    }
    if config.get("clean_predictions"):
        assets["clean_predictions"] = file_identity(config["clean_predictions"])
    if config.get("reference_protein_fasta"):
        assets["reference_protein_fasta"] = file_identity(config["reference_protein_fasta"])
    code_paths = [
        script.resolve(),
        repository / "src/GemAgents/tools.py",
        repository / "src/GemAgents/contracts.py",
        repository / "src/GemAgents/provenance.py",
        repository / "src/GemAgents/solver_result.py",
    ]
    return {
        "cache_kind": "reconstruction",
        "config": {k: v for k, v in config.items() if k not in {"output"}},
        "build_id": build["id"],
        "assets": assets,
        "runtime": runtime_identity(code_paths),
        "random_seed": config.get("random_seed", "not_used"),
    }


def run_key(config: dict, *, build: dict, script: Path) -> str:
    return stable_key(run_identity(config, build=build, script=script))


def save_status(
    path: Path,
    rows: list[dict],
    started: float,
    *,
    state: str = "running",
    total_builds: int | None = None,
) -> None:
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "status": state,
        "updated_unix": time.time(),
        "elapsed_seconds": time.time() - started,
        "counts": counts,
        "completed_builds": len(rows),
        "total_builds": total_builds if total_builds is not None else len(rows),
        "builds": rows,
    }
    atomic_json(path, payload)


def save_event(path: Path, events: list[dict], event: dict) -> None:
    events.append({"sequence": len(events) + 1, "unix": time.time(), **event})
    atomic_json(path, {"schema_version": SCHEMA_VERSION, "events": events})


def save_failure(output: Path, traceback_text: str) -> str | None:
    """Best effort diagnostics without replacing the reconstruction exception."""
    try:
        output.mkdir(parents=True, exist_ok=True)
        target = output / "batch-error.txt"
        target.write_text(traceback_text, encoding="utf-8")
        return None
    except OSError as error:
        return f"{type(error).__name__}: {error}"


def main() -> None:
    args = parse_args()
    workspace = workspace_from_args(args)
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    builds = validate_prepared_mapping(mapping)
    args.output_directory.mkdir(parents=True, exist_ok=True)
    status_path = args.output_directory / "batch-status.json"
    event_path = args.output_directory / "batch-events.json"
    started = time.time()
    rows = []
    events = []
    selected = [row for row in builds if row["status"] == "ready"]
    if args.only:
        selected = [row for row in selected if row["id"] in set(args.only)]
    if args.max_builds is not None:
        selected = selected[: args.max_builds]

    # Publish a running marker before asset hashing or the first genome build.
    # The supervisor must not confuse a partial status snapshot with a finished
    # batch when it polls this file.
    save_status(status_path, rows, started, total_builds=len(selected))

    # These assets are shared by every genome.  Hashing the HMM directory for
    # every row made cache validation needlessly reread roughly 1.3 GB.
    common_assets = {
        "reaction_library": file_identity(args.reaction_library),
        "biomass_library": file_identity(args.biomass_library),
        "hmm_directory": file_identity(args.hmm_directory),
        "source_policy": file_identity(workspace / "data/source-policy.yaml"),
    }

    with execution_lock(args.output_directory / ".batch.lock"):
        for index, build in enumerate(selected, 1):
            output_root = args.output_directory / "builds" / build["id"]
            output = output_root
            manifest_path = output / "manifest.json"
            clean_predictions = clean_predictions_for_build(args, build)
            config = {
                "input": build["sequence"],
                "input_type": "faa",
                "annotation": "ncbi-hmm",
                "allow_ambiguous_ec_gpr": True,
                "hmm_dir": str(args.hmm_directory.resolve()),
                "reaction_library": str(args.reaction_library.resolve()),
                "reaction_library_mode": "strict",
                "biomass_library": str(args.biomass_library.resolve()),
                "biomass_template": build["biomass_template"],
                "engine": "native",
                "kingdom": build["kingdom"],
                "gram": "unspecified",
                "medium": build["benchmark_medium"],
                "quality": "repair",
                "allow_non_growing_draft": False,
                # The matched published model may contain reactions without
                # complete formula/charge fields.  Permit those only in the
                # explicitly labelled public-reference fallback tier; native
                # NCBI/CLEAN evidence remains preferred.
                "allow_unverified_gapfill": True,
                # Use the matched published BiGG model only as a chemistry-preserving
                # high-cost gap-fill candidate pool.  It is not copied into the
                # sequence-supported initial model, and selected reactions retain
                # explicit public-reference provenance in the report/SBML notes.
                "reference_support": True,
                "reference_support_path": (
                    next(
                        row["model"]
                        for row in build.get("references", [])
                        if row["model_id"] == build["selected_reference_model_id"]
                    )
                    if build.get("references")
                    else None
                ),
                "reference_growth_ceiling": False,
                "evaluation_track": "reference_assisted",
                "reference_scaffold": True,
                # The benchmark sequence is also the reference protein set for
                # exact sequence based reference GPR transfer.
                "reference_protein_fasta": build["sequence"],
                "memote": False,
                "cpus": args.cpus,
                "output": str(output.resolve()),
                # Prevent apply_reconstruction_defaults from selecting the
                # repository-adjacent MG1655 table for unrelated genomes.
                "disable_clean_predictions_default": clean_predictions is None,
                "clean_predictions_require_overlap": clean_predictions is not None,
            }
            if clean_predictions is not None:
                config["clean_predictions"] = str(clean_predictions)
            config = apply_reconstruction_defaults(config, workspace)
            identity_payload = run_identity(
                config,
                build=build,
                script=Path(__file__),
                common_assets=common_assets,
            )
            expected_run_key = stable_key(identity_payload)
            identity = {
                "schema_version": SCHEMA_VERSION,
                "batch_run_key": expected_run_key,
                "build_id": build["id"],
                "evaluation_track": "reference_assisted",
                "run_identity": identity_payload,
                "effective_data_use": {
                    "evaluation_track": "reference_assisted",
                    "candidate_access": [build["selected_reference_model_id"]],
                    "selected_use": {
                        "reaction_and_gpr_gapfill": build["selected_reference_model_id"],
                        "biomass_source": build["biomass_source_model_id"],
                        "medium": build["selected_reference_model_id"],
                    },
                    "sequence_only_claim_allowed": False,
                },
            }
            cache_lineage = None
            original_identity_path = output_root.parent / f"{output_root.name}.batch-identity.json"
            if args.resume and manifest_path.is_file():
                valid, cache_status, manifest = validate_cache(manifest_path, expected_run_key)
                if not valid:
                    cache_lineage = {
                        "status": cache_status,
                        "previous_output": str(output.resolve()),
                        "previous_status": manifest.get("status") if manifest else None,
                    }
                    manifest = recover_unsealed_manifest(
                        manifest_path,
                        original_identity_path,
                        expected_run_key,
                        {
                            "resume_lineage": {
                                **cache_lineage,
                                "resumed_stage": "artifact_sealing",
                            },
                            "effective_data_use": identity["effective_data_use"],
                        },
                    )
                    valid = manifest is not None
                if valid:
                    row = {
                        "id": build["id"],
                        "organism": build["organism"],
                        "status": manifest.get("status"),
                        "reused": True,
                        "output": str(output.resolve()),
                        "elapsed_seconds": manifest.get("elapsed_seconds"),
                        "quality_status": manifest.get("quality_status"),
                        "evaluation_track": "reference_assisted",
                        "resume_lineage": manifest.get("resume_lineage"),
                    }
                    rows.append(row)
                    save_status(status_path, rows, started, total_builds=len(selected))
                    save_event(
                        event_path,
                        events,
                        {"build_id": build["id"], "event": "reused", "output": str(output)},
                    )
                    print(f"[{index}/{len(selected)}] cached {build['id']}", flush=True)
                    continue
            if output.exists() and any(output.iterdir()):
                output = output_root / "attempts" / uuid.uuid4().hex
                config["output"] = str(output.resolve())
                manifest_path = output / "manifest.json"
            identity_path = output.parent / f"{output.name}.batch-identity.json"
            identity.update(output=str(output.resolve()), resume_lineage=cache_lineage)

            print(
                f"[{index}/{len(selected)}] building {build['id']} {build['organism']}",
                flush=True,
            )
            build_started = time.time()
            try:
                atomic_json(identity_path, identity)
                save_event(
                    event_path,
                    events,
                    {
                        "build_id": build["id"],
                        "event": "started",
                        "output": str(output),
                        "resume_lineage": cache_lineage,
                    },
                )
                result = metabolic_pipeline(config, workspace)
                status = result["status"]
                error = None
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "batch_run_key": expected_run_key,
                        "cache_kind": "reconstruction",
                        "resume_lineage": cache_lineage,
                        "effective_data_use": identity["effective_data_use"],
                    }
                )
                manifest = seal_manifest(manifest_path, manifest)
            except Exception as exception:  # batch boundary: preserve every failure
                status = "failed"
                error = f"{type(exception).__name__}: {exception}"
                error_log_error = save_failure(output, traceback.format_exc())
            row = {
                "id": build["id"],
                "organism": build["organism"],
                "status": status,
                "output": str(output.resolve()),
                "elapsed_seconds": time.time() - build_started,
                "error": error,
                "error_log_error": error_log_error if error is not None else None,
                "resume_lineage": cache_lineage,
                "evaluation_track": "reference_assisted",
                "quality_status": result.get("quality_status") if error is None else None,
            }
            rows.append(row)
            save_status(status_path, rows, started, total_builds=len(selected))
            save_event(
                event_path,
                events,
                {
                    "build_id": build["id"],
                    "event": status,
                    "output": str(output),
                    "error": error,
                },
            )
            print(f"[{index}/{len(selected)}] {status} {build['id']}", flush=True)

    save_status(
        status_path,
        rows,
        started,
        state="completed",
        total_builds=len(selected),
    )


if __name__ == "__main__":
    main()
