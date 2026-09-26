"""Scale-aware numerical policy for quality conclusions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class NumericalPolicy:
    absolute_tolerance: float = 1e-7
    relative_tolerance: float = 1e-9
    critical_flux_tolerance: float = 1e-12
    solver: str = "unknown"

    def __post_init__(self) -> None:
        values = (
            self.absolute_tolerance,
            self.relative_tolerance,
            self.critical_flux_tolerance,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Numerical tolerances must be positive and finite")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def numerical_verdict(
    *,
    absolute_residual: float | None,
    scale: float | None,
    policy: NumericalPolicy,
    critical_flux: float | None = None,
) -> dict[str, object]:
    if absolute_residual is None or scale is None:
        return {"status": "numerical_inconclusive", "reason": "missing residual or scale"}
    if not math.isfinite(absolute_residual) or not math.isfinite(scale) or scale <= 0:
        return {"status": "numerical_inconclusive", "reason": "nonfinite residual or scale"}
    relative = absolute_residual / scale
    if critical_flux is not None and 0 < abs(critical_flux) <= policy.critical_flux_tolerance:
        return {
            "status": "numerical_inconclusive",
            "absolute_residual": absolute_residual,
            "relative_residual": relative,
            "reason": "critical flux is at numerical resolution",
        }
    passed = (
        absolute_residual <= policy.absolute_tolerance
        and relative <= policy.relative_tolerance
    )
    return {
        "status": "pass" if passed else "fail",
        "absolute_residual": absolute_residual,
        "relative_residual": relative,
    }
