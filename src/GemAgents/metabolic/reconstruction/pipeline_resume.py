"""Validated stage loading for deterministic reconstruction recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from GemAgents.errors import ToolError


def _artifact(bundle: dict[str, Any], name: str, *, root: Path) -> Path:
    paths = bundle.get("artifact_paths")
    if not isinstance(paths, dict) or not isinstance(paths.get(name), str):
        raise ToolError(
            f"recovery checkpoint is missing required reaction-mapping artifact: {name}"
        )
    path = Path(paths[name])
    if path.is_symlink() or not path.is_file():
        raise ToolError(f"recovery artifact is unavailable: {name}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ToolError(f"recovery artifact escapes the run output: {name}") from error
    return path


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ToolError(f"recovery artifact is invalid JSON: {label}") from error
    if not isinstance(payload, dict):
        raise ToolError(f"recovery artifact must be a JSON object: {label}")
    return payload


def _json_list(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ToolError(f"recovery artifact is invalid JSON: {label}") from error
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ToolError(f"recovery artifact must be a list of objects: {label}")
    return payload


def load_reaction_mapping_stage(
    bundle: dict[str, Any],
    *,
    config: dict[str, Any],
    out: Path,
    fasta_fn: Callable[[Path, str], list[tuple[str, str]]],
    native_universe_fn: Callable[[dict[str, Any]], tuple[Any, Path]],
    universe_fn: Callable[[dict[str, Any]], tuple[Any, Path]],
) -> dict[str, Any]:
    """Deserialize the completed reaction-mapping stage for a new worker.

    The loader only accepts the checkpoint phase that has all four immutable
    mapping artifacts.  It does not reconstruct a solver state; the next stage
    receives a freshly loaded reaction universe and must record a new phase
    checkpoint before doing model work.
    """
    if bundle.get("phase") != "reaction_mapping":
        raise ToolError(
            "only a reaction_mapping checkpoint can be resumed; "
            f"got {bundle.get('phase')!r}"
        )
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict):
        raise ToolError("recovery checkpoint manifest is invalid")
    expected_output = manifest.get("output")
    if isinstance(expected_output, str) and Path(expected_output).resolve() != out.resolve():
        raise ToolError("recovery manifest output does not match the job output")

    proteins_path = _artifact(bundle, "proteins.faa", root=out)
    annotation_path = _artifact(bundle, "annotation.json", root=out)
    gene_map_path = _artifact(bundle, "gene_id_map.json", root=out)
    mapped_path = _artifact(bundle, "reaction_evidence.json", root=out)
    clean_path = bundle.get("artifact_paths", {}).get("clean-predictions-selected.json")
    if isinstance(clean_path, str):
        clean_path = str(_artifact(bundle, "clean-predictions-selected.json", root=out))

    proteins = fasta_fn(proteins_path, "faa")
    evidence = _json_list(annotation_path, "annotation.json")
    gene_ids = _json_object(gene_map_path, "gene_id_map.json")
    mapped = _json_object(mapped_path, "reaction_evidence.json")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in gene_ids.items()):
        raise ToolError("recovery gene_id_map.json contains non-string IDs")
    clean_model_ids: set[str] = set()
    if isinstance(clean_path, str):
        clean_rows = _json_list(Path(clean_path), "clean-predictions-selected.json")
        for row in clean_rows:
            source_id = row.get("gene_id")
            if isinstance(source_id, str) and source_id in gene_ids:
                clean_model_ids.add(gene_ids[source_id])

    universe_loader = native_universe_fn if config.get("engine") == "native" else universe_fn
    universal, universe_path = universe_loader(config)
    return {
        "kind": str(manifest.get("input_detection", "unknown")),
        "annotation": str(manifest.get("annotation_method", "unknown")),
        "faa": proteins_path,
        "proteins": proteins,
        "evidence": evidence,
        "gene_ids": gene_ids,
        "clean_model_ids": clean_model_ids,
        "universal": universal,
        "universe_path": universe_path,
        "mapped": mapped,
    }


__all__ = ["load_reaction_mapping_stage"]
