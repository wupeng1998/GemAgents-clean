"""Canonical EC tokens shared by annotation and reaction evidence paths."""

from __future__ import annotations

import re

_EC_TOKEN = re.compile(
    r"(?<![0-9])(?:EC\s*:\s*)?([0-9]+|[-*])\.([0-9]+|[-*])\."
    r"([0-9]+|[-*])\.([0-9]+|[-*])(?![0-9])",
    re.IGNORECASE,
)


def metabolic_normalize_ec(value: object) -> str | None:
    """Return a canonical EC token, retaining partial ``-`` components."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _EC_TOKEN.fullmatch(text)
    if not match:
        return None
    return ".".join(
        "-" if part in {"-", "*"} else str(int(part)) for part in match.groups()
    )


def metabolic_normalize_ec_values(value: object) -> list[str]:
    """Extract sorted unique EC tokens from scalar or iterable input."""
    if value is None:
        return []
    raw_values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    tokens = []
    for raw in raw_values:
        for match in _EC_TOKEN.finditer(str(raw or "")):
            token = metabolic_normalize_ec(match.group(0))
            if token:
                tokens.append(token)
    return sorted(set(tokens))


def metabolic_ec_matches(pattern: str, value: str) -> bool:
    """Match exact or partial EC classes without broadening other fields."""
    left, right = metabolic_normalize_ec(pattern), metabolic_normalize_ec(value)
    if not left or not right:
        return False
    return all(
        a == "-" or b == "-" or a == b
        for a, b in zip(left.split("."), right.split("."), strict=True)
    )


__all__ = [
    "metabolic_ec_matches",
    "metabolic_normalize_ec",
    "metabolic_normalize_ec_values",
]
