"""Attempt-bound background heartbeat for reconstruction workers."""

from __future__ import annotations

import threading
from typing import Any


class WorkerHeartbeat:
    """Refresh a job's worker heartbeat while long domain stages are running."""

    def __init__(
        self,
        ledger: Any,
        job_id: str,
        attempt_id: str,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        if not 0 < interval_seconds <= 300:
            raise ValueError("heartbeat interval must be between 0 and 300 seconds")
        self.ledger = ledger
        self.job_id = job_id
        self.attempt_id = attempt_id
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker heartbeat already started")
        self.ledger.heartbeat(self.job_id, attempt_id=self.attempt_id)
        self._thread = threading.Thread(
            target=self._run,
            name=f"gemagents-heartbeat-{self.job_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.ledger.heartbeat(self.job_id, attempt_id=self.attempt_id)
            except (OSError, KeyError, TypeError, ValueError) as error:
                self.error = error
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(5.0, max(1.0, self.interval_seconds * 2)))


__all__ = ["WorkerHeartbeat"]
