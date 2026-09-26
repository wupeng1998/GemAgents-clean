"""Content addressed batch cache helpers.

The cache identity lives beside the batch status and is never passed to the
reconstruction contract as configuration.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2
REQUIRED_ARTIFACTS = ("model.xml", "quality.json", "gapfill-report.json")


def content_identity(value: str | Path) -> dict:
    path = Path(value)
    if path.is_dir():
        files = [content_identity(p) for p in sorted(path.rglob("*")) if p.is_file()]
        return {
            "path": str(path.resolve()),
            "files": files,
            "sha256": stable_key({"files": files}),
        }
    if not path.exists() or not path.is_file():
        return {"path": str(path.resolve()), "status": "missing"}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def runtime_identity(code_paths: list[Path]) -> dict:
    """Return code and solver identities that can change reconstruction semantics."""
    packages = {}
    for package in ("cobra", "optlang", "swiglpk", "scipy", "numpy"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = "not-installed"
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "packages": packages,
        "code": [content_identity(path) for path in code_paths],
    }


def stable_key(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def execution_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, ImportError) as error:
        handle.close()
        raise RuntimeError("batch execution lease is already held") from error
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def artifact_hashes(output: Path) -> dict[str, str]:
    """Hash every completed output artifact except the self-referential manifest."""
    hashes = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path != output / "manifest.json":
            hashes[path.relative_to(output).as_posix()] = content_identity(path)["sha256"]
    return hashes


def seal_manifest(manifest_path: Path, manifest: dict) -> dict:
    """Bind a completed manifest to all output bytes before cache publication."""
    output = manifest_path.parent
    missing = [name for name in REQUIRED_ARTIFACTS if not (output / name).is_file()]
    if missing:
        raise ValueError(f"cannot seal incomplete reconstruction artifacts: {missing}")
    sealed = dict(manifest)
    sealed["artifact_hashes"] = artifact_hashes(output)
    atomic_json(manifest_path, sealed)
    return sealed


def recover_unsealed_manifest(
    manifest_path: Path,
    identity_path: Path,
    expected_key: str,
    updates: dict,
) -> dict | None:
    """Publish a completed pre-seal result after an interruption, without rebuilding it."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if manifest.get("artifact_hashes"):
        return None
    if (
        identity.get("schema_version") != SCHEMA_VERSION
        or identity.get("batch_run_key") != expected_key
        or manifest.get("status") not in {"completed", "completed_with_findings"}
        or manifest.get("execution_status", "completed") != "completed"
    ):
        return None
    if any(not (manifest_path.parent / name).is_file() for name in REQUIRED_ARTIFACTS):
        return None
    recovered = {
        **manifest,
        **updates,
        "schema_version": SCHEMA_VERSION,
        "batch_run_key": expected_key,
        "cache_kind": "reconstruction",
    }
    return seal_manifest(manifest_path, recovered)


def validate_cache(manifest_path: Path, expected_key: str) -> tuple[bool, str, dict | None]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "CACHE_UNVERIFIED", None
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("batch_run_key") != expected_key
        or manifest.get("cache_kind") != "reconstruction"
    ):
        return False, "CACHE_UNVERIFIED", manifest
    if manifest.get("status") not in {"completed", "completed_with_findings"}:
        return False, "CACHE_UNVERIFIED", manifest
    output = manifest_path.parent
    expected = manifest.get("artifact_hashes")
    if not isinstance(expected, dict) or not expected:
        return False, "CACHE_UNVERIFIED", manifest
    if any(name not in expected for name in REQUIRED_ARTIFACTS):
        return False, "CACHE_UNVERIFIED", manifest
    if artifact_hashes(output) != expected:
        return False, "CACHE_UNVERIFIED", manifest
    return True, "reused", manifest
