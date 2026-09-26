"""Auditable metrics for context compaction without storing conversation text."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any


def _units(messages: Iterable[Any]) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", "")
        total += len(json.dumps(content, ensure_ascii=False, default=str))
    return total


def _estimated_tokens(units: int) -> int:
    return math.ceil(units / 4) if units else 0


def _constraint_count(messages: Iterable[Any]) -> int:
    count = 0
    for message in messages:
        message_type = getattr(message, "type", None)
        content = str(getattr(message, "content", ""))
        if message_type == "human":
            count += 1
        elif "Archived user constraints:" in content:
            count += sum(line.startswith("- ") for line in content.splitlines())
    return count


def compaction_metrics(
    original: list[Any], compacted: list[Any], elapsed_ms: float
) -> dict[str, object]:
    original_constraints = _constraint_count(original)
    compacted_constraints = _constraint_count(compacted)
    original_units = _units(original)
    compacted_units = _units(compacted)
    return {
        "original_messages": len(original),
        "compacted_messages": len(compacted),
        "estimated_original_tokens": _estimated_tokens(original_units),
        "estimated_compacted_tokens": _estimated_tokens(compacted_units),
        "estimated_tokens_saved": _estimated_tokens(original_units - compacted_units),
        "elapsed_ms": round(max(0.0, elapsed_ms), 3),
        "constraints_total": original_constraints,
        "constraints_retained": compacted_constraints,
        "constraint_retention": (
            compacted_constraints / original_constraints if original_constraints else 1.0
        ),
    }
