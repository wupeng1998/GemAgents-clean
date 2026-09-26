from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MarkdownDocument:
    metadata: dict[str, object]
    body: str


def read_markdown_document(path: Path) -> MarkdownDocument:
    text = path.read_text(encoding="utf-8")
    return parse_markdown_document(text)


def parse_markdown_document(text: str) -> MarkdownDocument:
    if not text.startswith("---\n"):
        return MarkdownDocument(metadata={}, body=text)

    end = text.find("\n---\n", 4)
    if end == -1:
        return MarkdownDocument(metadata={}, body=text)

    raw_metadata = text[4:end]
    body = text[end + 5 :]
    loaded = yaml.safe_load(raw_metadata) or {}
    if not isinstance(loaded, dict):
        loaded = {}
    return MarkdownDocument(metadata=loaded, body=body)

