#!/usr/bin/env python3
"""Index visible historical evidence without rerunning or modifying old runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

from GemAgents.layout import RepoLayout

ROOT = Path(__file__).resolve().parents[1]


def files_under(directory):
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if not (Path(base) / d).is_symlink() and d.lower() not in {"mqc", "pear", "__pycache__"}
        ]
        for name in names:
            path = Path(base) / name
            if not path.is_symlink() and not re.search(r"(?i)(mqc|pear)", name):
                yield path


def evidence(path, root=ROOT):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path.relative_to(root)),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def build_index(root=ROOT):
    root = Path(root).resolve()
    sources = [root / "research-log.md", root / "research-state.yaml"]
    sources += sorted((root / "docs").glob("*validation*"))
    sources += sorted((root / "experiments").glob("**/results/*.json"))
    sources += sorted(root.glob("*.json"))
    records = {}

    def entry(name):
        return records.setdefault(name, {"run_id": name, "references": [], "artifacts": []})

    for source in sources:
        if not source.is_file() or source.is_symlink():
            continue
        text = source.read_text(encoding="utf-8")
        kind = (
            "CONFIG"
            if source.parent == ROOT and source.suffix == ".json"
            else "SUMMARY"
            if source.suffix == ".json"
            else "TEXT"
        )
        names = set(re.findall(r"runs[/\\]([A-Za-z0-9_.-]+)", text))
        for name in sorted(names):
            entry(name)["references"].append({**evidence(source, root), "kind": kind})
        if kind == "SUMMARY" and not names:
            entry("summary:" + str(source.relative_to(root)))["references"].append(
                {**evidence(source, root), "kind": kind}
            )
    runs_root = RepoLayout(root).runs
    for path in files_under(runs_root):
        relative = path.relative_to(runs_root)
        if len(relative.parts) == 1:
            # Top-level logs/job reports have their own evidence entry.
            name = relative.stem
        else:
            # Batch outputs have one child per strain; retain the subdirectory identity.
            name = str(relative.parent) if len(relative.parts) > 2 else relative.parts[0]
        if path.suffix not in {".xml", ".json", ".log", ".txt"}:
            continue
        record = entry(name)
        item = evidence(path, root)
        item["kind"] = (
            "MODEL"
            if path.suffix == ".xml"
            else "LOG"
            if path.suffix in {".log", ".txt"}
            else "REPORT"
        )
        record["artifacts"].append(item)
        if path.name == "manifest.json":
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                record["recorded_status"] = manifest.get("status")
                record["recorded_status_scope"] = "historical claim, not current validation"
            except (ValueError, AttributeError) as exc:
                record["manifest_error"] = str(exc)
    for record in records.values():
        artifacts = record["artifacts"]
        model = any(a["kind"] == "MODEL" and a["size_bytes"] for a in artifacts)
        log = any(a["kind"] == "LOG" and a["size_bytes"] for a in artifacts)
        refs = {r["kind"] for r in record["references"]}
        record["raw_model_visible"] = bool(model)
        record["raw_log_visible"] = bool(log)
        record["raw_artifacts_missing"] = not (model and log)
        record["classification"] = (
            "RAW_MODEL_AND_LOG_VISIBLE"
            if model and log
            else "RAW_MODEL_VISIBLE_LOG_MISSING"
            if model
            else "SUMMARY_ONLY"
            if artifacts or "SUMMARY" in refs
            else "CONFIG_ONLY"
            if refs == {"CONFIG"}
            else "TEXT_ONLY"
            if "TEXT" in refs
            else "RAW_ARTIFACTS_MISSING"
        )
        record["rerun_performed"] = False
    result = {
        "schema_version": 1,
        "scope": "Visible runs and identifiers from research and validation records",
        "limitations": [
            "Existence and hashes do not establish model correctness.",
            "Historical status claims are not independently revalidated.",
            "Partial stage models are not proof of an exported final model.",
        ],
        "summary": dict(Counter(r["classification"] for r in records.values())),
        "runs": [records[k] for k in sorted(records)],
    }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/audit/historical-run-index.json")
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError as error:
        raise SystemExit("--output must be inside --root") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    result = build_index(root)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], sort_keys=True))
