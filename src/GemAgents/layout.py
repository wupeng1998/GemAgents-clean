"""Explicit repository paths without dependence on the process cwd."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def resolve_workspace(value: str | Path | None = None) -> Path:
    """Resolve an explicit workspace or the caller's cwd in one place."""
    return (Path(value).expanduser() if value is not None else Path.cwd()).resolve()


@dataclass(frozen=True)
class RepoLayout:
    root: Path
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        target = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"path escapes repository: {value}") from error
        return target

    @property
    def runtime(self) -> Path:
        return self.resolve(".gemagents")

    @property
    def contracts(self) -> Path:
        return self.resolve(".gemagents/contracts")

    @property
    def jobs(self) -> Path:
        return self.resolve(".gemagents/metabolic_jobs")

    @property
    def runs(self) -> Path:
        return self.resolve("runs")

    @property
    def audit(self) -> Path:
        return self.resolve("artifacts/audit")

    @property
    def assets(self) -> Path:
        return self.resolve("data")

    @property
    def archive(self) -> Path:
        return self.resolve("artifacts/archive")

    @property
    def configs(self) -> Path:
        return self.root

    def writable(self, value: str | Path) -> Path:
        if self.read_only:
            raise PermissionError("workspace is read-only")
        raw = Path(value).expanduser()
        candidate = self.root / raw if not raw.is_absolute() else raw
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            relative = None
        if relative is not None:
            current = self.root
            for component in relative.parts:
                current /= component
                if current.is_symlink():
                    raise ValueError("write target path must not contain a symlink")
        elif candidate.is_symlink():
            raise ValueError("write target must not be a symlink")
        target = self.resolve(value)
        parent = target.parent
        if parent.exists() and parent.is_symlink():
            raise ValueError("write target parent must not be a symlink")
        return target


@dataclass(frozen=True)
class UserLayout:
    """Read-only paths under the user's GemAgents config root."""

    root: Path | None = None

    def __post_init__(self) -> None:
        raw = Path.home() / ".gemagents" if self.root is None else Path(self.root)
        object.__setattr__(self, "root", raw.expanduser().resolve())

    def resolve(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        target = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"user path escapes compatibility root: {value}") from error
        return target

    @property
    def settings(self) -> Path:
        return self.resolve("settings.json")

    def extensions(self, dirname: str) -> Path:
        return self.resolve(dirname)


@dataclass(frozen=True)
class ArtifactLocator:
    """Resolve versioned asset IDs and legacy aliases inside one repository root."""

    layout: RepoLayout
    index_path: Path | None = None

    def _index(self) -> dict[str, Any]:
        path = self.index_path or self.layout.resolve("artifacts/index/assets.json")
        if not path.is_file() or path.is_symlink():
            return {"assets": [], "aliases": {}}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("assets", []), list):
            raise ValueError("asset index must contain an assets list")
        if not isinstance(payload.get("aliases", {}), dict):
            raise ValueError("asset index aliases must be an object")
        return payload

    def resolve(self, locator: str) -> dict[str, object]:
        payload = self._index()
        aliases = payload.get("aliases", {})
        asset_id = str(aliases.get(locator, locator))
        if asset_id.startswith("asset://"):
            asset_id = asset_id.removeprefix("asset://")
        matches = [
            row
            for row in payload["assets"]
            if isinstance(row, dict) and row.get("asset_id") == asset_id
        ]
        if len(matches) != 1:
            try:
                target = self.layout.resolve(locator)
            except ValueError:
                return {"status": "BLOCKED_PATH_ESCAPE", "locator": locator}
            if target.is_file() and not target.is_symlink():
                return {
                    "status": "PRESENT",
                    "locator": locator,
                    "path": str(target.relative_to(self.layout.root)),
                    "sha256": _sha256(target),
                    "via": "legacy_path",
                }
            return {"status": "UNRESOLVED", "locator": locator}
        row = matches[0]
        path = row.get("path")
        expected = row.get("sha256")
        if not isinstance(path, str) or not isinstance(expected, str):
            raise ValueError("asset index rows require path and sha256")
        try:
            target = self.layout.resolve(path)
        except ValueError:
            return {"status": "BLOCKED_PATH_ESCAPE", "locator": locator}
        if not target.is_file() or target.is_symlink():
            return {"status": "MISSING_TARGET", "locator": locator, "path": path}
        actual = _sha256(target)
        if actual != expected:
            return {
                "status": "HASH_MISMATCH",
                "locator": locator,
                "asset_id": asset_id,
                "path": path,
                "expected_sha256": expected,
                "actual_sha256": actual,
            }
        return {
            "status": "RESOLVED",
            "locator": locator,
            "asset_id": asset_id,
            "path": path,
            "sha256": actual,
            "via": "versioned_asset_index",
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
