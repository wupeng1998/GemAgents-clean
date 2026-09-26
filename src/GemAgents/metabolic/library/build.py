"""Reproducible reaction library build recipe."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BuildRecipe:
    builder_version: str
    source_versions: Mapping[str, str]
    source_hashes: Mapping[str, str]
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.builder_version:
            raise ValueError("builder_version is required")
        if not self.source_versions or set(self.source_versions) != set(self.source_hashes):
            raise ValueError("Every build source requires both a version and a content hash")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
