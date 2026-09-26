#!/usr/bin/env python3
"""Create a read-only inventory of GemAgents assets.

The scanner deliberately has a small, explicit scope.  It never follows a
symlink outside the repository and never opens paths classified as restricted
(``MQC``/``pear``).  It is intended for T00 inventories, not for rebuilding a
model or downloading missing databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout

RESTRICTED_RE = re.compile(r"(?:^|[\\/_.-])(mqc|pear)(?:$|[\\/_.-])", re.I)
ASSET_EXTENSIONS = {
    ".fa",
    ".faa",
    ".fna",
    ".fasta",
    ".gb",
    ".gbk",
    ".gbff",
    ".hmm",
    ".tgz",
    ".gz",
    ".xml",
    ".json",
    ".jsonl",
    ".tsv",
    ".yaml",
    ".yml",
}
GENOME_EXTENSIONS = {".fa", ".faa", ".fna", ".fasta", ".gb", ".gbk", ".gbff"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_restricted(path: Path, root: Path) -> bool:
    return bool(RESTRICTED_RE.search(relpath(path, root)))


def iter_candidates(root: Path) -> Iterator[Path]:
    """Yield relevant repository files without traversing generated runs."""
    scan_roots = [root / name for name in ("data", "bigg", "carveme", "reconstructor")]
    scan_roots += sorted(root.glob("*.json"))
    for scan_root in scan_roots:
        if scan_root.is_symlink() or scan_root.is_file():
            yield scan_root
            continue
        if not scan_root.is_dir():
            continue
        for directory, dirnames, filenames in os.walk(scan_root, followlinks=False):
            directory_path = Path(directory)
            for name in list(dirnames):
                path = directory_path / name
                if path.is_symlink() or is_restricted(path, root):
                    yield path
                    dirnames.remove(name)
                elif name in {".git", "__pycache__", "build"} or name.endswith(".egg-info"):
                    dirnames.remove(name)
            for filename in filenames:
                path = directory_path / filename
                if (
                    path.is_symlink()
                    or scan_root.name in {"data", "bigg"}
                    or path.suffix.lower() in ASSET_EXTENSIONS
                ):
                    yield path


def classify(path: Path, root: Path) -> str:
    normalized = relpath(path, root).lower()
    if is_restricted(path, root):
        return "restricted_policy_excluded"
    if path.suffix.lower() in GENOME_EXTENSIONS:
        return "genome_or_protein_input"
    if path.suffix.lower() == ".hmm" or "ncbi_hmm" in normalized:
        return "ncbi_hmm_or_annotation_database"
    if "reaction_library" in normalized or "universe" in path.name.lower():
        return "reaction_library"
    if "biomass" in normalized or "biomass" in path.name.lower():
        return "biomass_catalog_or_source_model"
    if "pgap" in normalized:
        return "external_tool_or_database_record"
    if path.suffix.lower() in {".xml", ".gz"}:
        return "source_model_or_compiled_model"
    return "metadata_or_manifest"


def fasta_findings(path: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    identifiers: dict[str, int] = defaultdict(int)
    try:
        with path.open("rt", encoding="utf-8") as handle:
            saw_record = False
            sequence_length = 0
            allowed = (
                set("ACGTURYSWKMBDHVN")
                if path.suffix.lower() == ".fna"
                else set("ABCDEFGHIJKLMNOPQRSTUVWXYZ*-.")
            )
            for line_number, line in enumerate(handle, 1):
                if line.startswith(">"):
                    if saw_record and not sequence_length:
                        findings.append({"code": "EMPTY_SEQUENCE", "detail": str(line_number)})
                    identifier = line[1:].strip().split(None, 1)[0] if line[1:].strip() else ""
                    identifiers[identifier] += 1
                    saw_record = True
                    sequence_length = 0
                elif line.strip():
                    if not saw_record or not set(line.strip().upper()) <= allowed:
                        findings.append({"code": "INVALID_FASTA", "detail": str(line_number)})
                        break
                    sequence_length += len(line.strip())
            if not saw_record:
                findings.append({"code": "INVALID_FASTA", "detail": "no FASTA record header"})
            elif not sequence_length:
                findings.append({"code": "EMPTY_SEQUENCE", "detail": "last record"})
    except (OSError, UnicodeError) as exc:
        findings.append({"code": "READ_ERROR", "detail": str(exc)})
    for identifier, count in sorted(identifiers.items()):
        if not identifier:
            findings.append({"code": "EMPTY_FASTA_ID", "detail": "empty record identifier"})
        elif count > 1:
            findings.append(
                {"code": "DUPLICATE_FASTA_ID", "detail": f"{identifier!r} occurs {count} times"}
            )
    return findings


def git_value(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def inspect_asset(path: Path, root: Path) -> dict[str, Any]:
    relative = relpath(path, root)
    record: dict[str, Any] = {
        "path": relative,
        "category": classify(path, root),
        "sha256": None,
        "version": "UNKNOWN",
        "source_status": "BLOCKED_PROVENANCE",
        "source": "Local file; upstream provenance requires linked source records",
    }
    try:
        stat = path.lstat()
        record.update({"size_bytes": stat.st_size, "is_symlink": path.is_symlink()})
        if path.is_symlink():
            target = path.resolve(strict=False)
            record["symlink_target"] = os.readlink(path)
            record["symlink_within_root"] = target == root or root in target.parents
            if not record["symlink_within_root"]:
                record["status"] = "BLOCKED_SYMLINK_ESCAPE"
                record["findings"] = [{"code": "SYMLINK_ESCAPE", "detail": str(target)}]
                return record
            record["status"] = "BLOCKED_SYMLINK"
            record["findings"] = [{"code": "SYMLINK_NOT_FOLLOWED", "detail": "read-only policy"}]
            return record
        if record["category"] == "restricted_policy_excluded":
            record.update(
                {
                    "status": "BLOCKED_POLICY",
                    "sha256": None,
                    "findings": [
                        {"code": "RESTRICTED_SOURCE", "detail": "path excluded by AGENT.md policy"}
                    ],
                }
            )
            return record
        if stat.st_size > 2 * 1024**3:
            record["status"] = "BLOCKED_ASSET"
            record["findings"] = [{"code": "HASH_NOT_RUN", "detail": "over 2 GiB; not mounted"}]
            return record
        record["sha256"] = sha256_file(path)
        findings: list[dict[str, str]] = []
        if stat.st_size == 0:
            findings.append({"code": "ZERO_BYTE", "detail": "file is empty"})
        if path.suffix.lower() in {".fa", ".faa", ".fna", ".fasta"}:
            findings.extend(fasta_findings(path))
        record["findings"] = findings
        record["status"] = "AVAILABLE" if not findings else "AVAILABLE_WITH_FINDINGS"
    except FileNotFoundError as exc:
        record.update(
            {
                "status": "BLOCKED_ASSET",
                "sha256": None,
                "findings": [{"code": "MISSING_ASSET", "detail": str(exc)}],
            }
        )
    except OSError as exc:
        record.update(
            {
                "status": "BLOCKED_READ",
                "sha256": None,
                "findings": [{"code": "READ_ERROR", "detail": str(exc)}],
            }
        )
    return record


PATH_KEYS = {
    "input",
    "annotation_gbk",
    "reaction_library",
    "biomass_library",
    "reference_support_path",
    "model",
    "path",
    "file",
    "reference_fna",
    "reference_genbank",
    "clean_predictions",
    "hmm_dir",
    "modelseed_reactions",
    "launcher",
    "home",
}


def attach_metadata(assets: list[dict], root: Path) -> list[dict]:
    """Record declared provenance and stale paths, without following referenced files."""
    by_path = {a["path"]: a for a in assets}
    references = []

    def walk(value, source, key_path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                location = f"{key_path}/{key}"
                if key in PATH_KEYS and isinstance(item, str) and item:
                    target = Path(item)
                    if RESTRICTED_RE.search(item):
                        status = "BLOCKED_POLICY"
                    elif target.is_absolute() or re.match(r"^[A-Za-z]:", item):
                        status = "BLOCKED_EXTERNAL_PATH"
                    else:
                        # Historical basename references are relative to their manifest.
                        candidate = root / item if "/" in item else source.parent / item
                        resolved = candidate.resolve(strict=False)
                        if not resolved.is_relative_to(root):
                            status = "BLOCKED_SYMLINK_ESCAPE"
                        elif candidate.is_symlink():
                            status = "BLOCKED_SYMLINK"
                        else:
                            status = "PRESENT" if candidate.exists() else "BLOCKED_ASSET"
                    references.append(
                        {
                            "source": str(source.relative_to(root)),
                            "field": location,
                            "value": item,
                            "status": status,
                        }
                    )
                walk(item, source, location)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, source, f"{key_path}/{index}")

    for asset in assets:
        path = root / asset["path"]
        if not asset.get("sha256"):
            continue
        metadata = []
        for name in ("source.json", "manifest.json", "download-manifest.json", "enzyme-index.json"):
            candidate = path.parent / name
            relative = str(candidate.relative_to(root))
            if relative in by_path and by_path[relative].get("sha256"):
                metadata.append({"path": relative, "sha256": by_path[relative]["sha256"]})
        if "biomass_registry_cache/bigg_models/" in asset["path"]:
            for name in (
                "data/public_biomass_registry.json",
                "data/public_biomass_registry_downloaded.json",
            ):
                if name in by_path and by_path[name].get("sha256"):
                    metadata.append({"path": name, "sha256": by_path[name]["sha256"]})
        asset["source_records"] = metadata
        if metadata:
            asset["source_status"] = "DECLARED_NOT_INDEPENDENTLY_VERIFIED"
            asset["source"] = "Declared provenance in hashed source_records; T04 audit pending"
        match = re.search(r"(?:_v(\d+)|[NG]C[FA]?_\d+\.\d+)", asset["path"])
        if match:
            asset["version"] = match.group(0)
            asset["version_basis"] = "filename label, not independently verified"
        if path.suffix == ".json" and (
            path.parent == root
            or path.name
            in {
                "manifest.json",
                "source.json",
                "download-manifest.json",
                "runtime.json",
                "iml1515_spec.json",
                "biomass_spec.json",
            }
        ):
            try:
                contents = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                asset["findings"].append({"code": "INVALID_JSON", "detail": str(exc)})
                asset["status"] = "AVAILABLE_WITH_FINDINGS"
                continue
            walk(contents, path)
    return references


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    assets = [inspect_asset(path, root) for path in sorted(set(iter_candidates(root)))]
    expected = [
        ("data/ncbi_hmm/enzymes.hmm", "ncbi_hmm_or_annotation_database"),
        ("data/reaction_library_v6/manifest.json", "reaction_library"),
        ("data/prokaryotic_biomass_library/manifest.json", "biomass_catalog_or_source_model"),
        ("data/pgap/runtime.json", "external_tool_or_database_record"),
        ("data/public_ecoli/NC_000913.3.gbff", "genome_or_protein_input"),
        ("data/public_mgen/NC_000908.2.gbff", "genome_or_protein_input"),
    ]
    by_path = {item["path"]: item for item in assets}
    for expected_path, category in expected:
        if expected_path not in by_path:
            assets.append(
                {
                    "path": expected_path,
                    "category": category,
                    "status": "BLOCKED_ASSET",
                    "size_bytes": None,
                    "sha256": None,
                    "findings": [
                        {"code": "MISSING_ASSET", "detail": "expected project asset is absent"}
                    ],
                }
            )
    references = attach_metadata(assets, root)
    duplicates: dict[str, list[str]] = defaultdict(list)
    for asset in assets:
        if asset.get("sha256"):
            duplicates[asset["sha256"]].append(asset["path"])
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "root": str(root),
        "git": {
            "head": git_value(root, "rev-parse", "HEAD"),
            "branch": git_value(root, "branch", "--show-current"),
            "status_porcelain": git_value(root, "status", "--porcelain"),
        },
        "environment": {"python": sys.version, "platform": platform.platform()},
        "policy": {
            "restricted_path_regex": RESTRICTED_RE.pattern,
            "restricted_paths_are_not_opened": True,
            "scope": "repository data/bigg/carveme/reconstructor asset paths",
        },
        "assets": sorted(assets, key=lambda item: item["path"]),
        "identical_content": [
            {"sha256": digest, "paths": paths}
            for digest, paths in sorted(duplicates.items())
            if len(paths) > 1
        ],
        "path_references": references,
        "quarantine": [a["path"] for a in assets if a.get("findings")],
        "summary": {
            "total": len(assets),
            "available": sum(item.get("status", "").startswith("AVAILABLE") for item in assets),
            "blocked": sum(item.get("status", "").startswith("BLOCKED") for item in assets),
            "findings": sum(bool(item.get("findings")) for item in assets),
        },
    }
    output = args.output
    if output is None:
        output = RepoLayout(root).audit / "assets.lock.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
