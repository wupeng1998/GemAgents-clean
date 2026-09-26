"""Datetime names available on the supported Python runtimes."""

from datetime import datetime, timezone
from enum import Enum

try:  # Python 3.11+
    from datetime import UTC
except ImportError:  # pragma: no cover - exercised on Python 3.10
    UTC = timezone.utc  # noqa: UP017

try:  # Python 3.11+
    from enum import StrEnum
except ImportError:  # pragma: no cover - exercised on Python 3.10
    # The functional form keeps the fallback equivalent to ``enum.StrEnum``
    # without relying on syntax that Ruff quite correctly flags on 3.11+.
    StrEnum = Enum(
        "StrEnum",
        {"__str__": str.__str__, "__module__": __name__},
        type=str,
    )

__all__ = ["UTC", "StrEnum", "datetime"]
