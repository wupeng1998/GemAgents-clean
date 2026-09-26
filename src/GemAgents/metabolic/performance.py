"""Small, dependency-free performance measurements for reconstruction stages."""

from __future__ import annotations

import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any


@dataclass(frozen=True)
class PerformanceSample:
    stage: str
    seconds: float
    cache_state: str = "cold"
    metadata: dict[str, Any] = field(default_factory=dict)


class PerformanceProfiler:
    def __init__(self) -> None:
        self.samples: list[PerformanceSample] = []

    @contextmanager
    def measure(
        self,
        stage: str,
        *,
        cache_state: str = "cold",
        **metadata: Any,
    ) -> Iterator[None]:
        if not stage or cache_state not in {"cold", "hot", "uncached"}:
            raise ValueError("stage is required and cache_state must be cold, hot or uncached")
        started = perf_counter()
        try:
            yield
        finally:
            self.samples.append(
                PerformanceSample(stage, perf_counter() - started, cache_state, dict(metadata))
            )

    def record(
        self,
        stage: str,
        seconds: float,
        *,
        cache_state: str = "cold",
        **metadata: Any,
    ) -> PerformanceSample:
        if seconds < 0:
            raise ValueError("seconds must be nonnegative")
        sample = PerformanceSample(stage, seconds, cache_state, dict(metadata))
        self.samples.append(sample)
        return sample

    def summary(self) -> dict[str, dict[str, Any]]:
        grouped: dict[tuple[str, str], list[float]] = {}
        for sample in self.samples:
            grouped.setdefault((sample.stage, sample.cache_state), []).append(sample.seconds)
        result = {}
        for (stage, cache_state), values in sorted(grouped.items()):
            result[f"{stage}:{cache_state}"] = {
                "n": len(values),
                "median_seconds": statistics.median(values),
                "min_seconds": min(values),
                "max_seconds": max(values),
            }
        return result

    def as_dict(self) -> dict[str, Any]:
        return {"samples": [asdict(sample) for sample in self.samples], "summary": self.summary()}


def profile_call(
    profiler: PerformanceProfiler,
    stage: str,
    function,
    *args,
    cache_state: str = "cold",
    **kwargs,
):
    with profiler.measure(stage, cache_state=cache_state):
        return function(*args, **kwargs)
