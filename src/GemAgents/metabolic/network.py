"""Bounded network IO for public metabolic reference assets."""

from __future__ import annotations

from pathlib import Path

import requests


def metabolic_download(url: str, path: Path) -> None:
    """Download a public reference asset atomically with bounded timeouts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with requests.get(url, stream=True, timeout=(20, 60)) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                handle.write(chunk)
    part.replace(path)
