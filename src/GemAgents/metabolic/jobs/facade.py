"""Public job-operation facade without importing the historical reconstruction module."""

from __future__ import annotations

import subprocess
from typing import Any

from GemAgents.metabolic.contracts import metabolic_validate_options
from GemAgents.metabolic.io import metabolic_json
from GemAgents.metabolic.jobs.batch import (
    metabolic_batch_start,
    metabolic_batch_status,
)
from GemAgents.metabolic.jobs.contracts import compile_contract
from GemAgents.metabolic.jobs.runtime import metabolic_cancel, metabolic_status
from GemAgents.metabolic.jobs.submit import (
    metabolic_resume as _metabolic_resume,
)
from GemAgents.metabolic.jobs.submit import (
    metabolic_start as _metabolic_start,
)


def metabolic_start(
    runner: Any,
    config_path: str,
    *,
    contract: dict[str, object] | None = None,
    write_json_fn=metabolic_json,
    popen_fn=subprocess.Popen,
    validate_options_fn=metabolic_validate_options,
    compile_contract_fn=compile_contract,
) -> str:
    """Submit a reconstruction job through the jobs layer's stable API."""
    return _metabolic_start(
        runner,
        config_path,
        contract=contract,
        validate_options_fn=validate_options_fn,
        compile_contract_fn=compile_contract_fn,
        write_json_fn=write_json_fn,
        popen_fn=popen_fn,
    )


def metabolic_resume(runner: Any, job_id: str) -> str:
    """Resume a verified checkpoint through the jobs layer's stable API."""
    return _metabolic_resume(runner, job_id)


__all__ = [
    "metabolic_batch_start",
    "metabolic_batch_status",
    "metabolic_cancel",
    "metabolic_resume",
    "metabolic_start",
    "metabolic_status",
]
