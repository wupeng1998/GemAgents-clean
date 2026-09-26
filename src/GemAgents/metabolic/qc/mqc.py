"""Optional adapter for the user-authorized external MQC implementation.

The adapter keeps MQC execution outside the native quality implementation.  It
records the external command, raw MQC outputs, model hash, and an independent
biomass FBA/pFBA readback so an MQC report cannot silently replace GemAgents'
own quality certificate.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from GemAgents.errors import ToolError


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_summary(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError(f"MQC check result is not valid JSON: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ToolError(f"MQC check result must be an object: {path}")
    summary = {}
    for name, value in payload.items():
        if not isinstance(value, dict):
            continue
        summary[str(name)] = {
            "score": value.get("score"),
            "summary": value.get("Summary", value.get("summary", "")),
        }
    return summary


def _biomass_simulation(model_path: Path) -> dict[str, Any]:
    try:
        from cobra.flux_analysis import pfba
        from cobra.io import read_sbml_model
    except ImportError as error:  # pragma: no cover - optional dependency
        raise ToolError("MQC biomass readback requires COBRApy") from error
    model = read_sbml_model(str(model_path))
    objective_ids = [
        reaction.id
        for reaction in model.reactions
        if abs(float(reaction.objective_coefficient)) > 1e-12
    ]
    boundary_objectives = [
        reaction_id
        for reaction_id in objective_ids
        if reaction_id.upper().startswith(("EX_", "DM_", "SK_", "R_EX_", "R_DM_", "R_SK_"))
    ]
    if boundary_objectives:
        return {
            "model_dimensions": {
                "reactions": len(model.reactions),
                "metabolites": len(model.metabolites),
                "genes": len(model.genes),
                "medium_exchange_count": len(model.medium),
            },
            "objective": objective_ids,
            "objective_validation": {
                "status": "invalid_boundary_objective",
                "boundary_objectives": boundary_objectives,
                "policy": (
                    "MQC output is not treated as a biomass model when its default "
                    "objective is EX/DM/SK"
                ),
            },
            "fba": {"solver_status": "not_run", "biomass_objective_value": None},
            "pfba": {
                "solver_status": "not_run",
                "biomass_objective_value": None,
                "total_absolute_flux": None,
                "cobra_pfba_objective_value": None,
            },
        }
    fba = model.optimize()
    p_result = pfba(model)
    objective = objective_ids[0] if len(objective_ids) == 1 else None
    return {
        "model_dimensions": {
            "reactions": len(model.reactions),
            "metabolites": len(model.metabolites),
            "genes": len(model.genes),
            "medium_exchange_count": len(model.medium),
        },
        "objective": objective_ids,
        "objective_validation": {"status": "accepted_for_readback", "boundary_objectives": []},
        "fba": {
            "solver_status": fba.status,
            "biomass_objective_value": float(fba.objective_value),
        },
        "pfba": {
            "solver_status": p_result.status,
            "biomass_objective_value": (
                float(p_result.fluxes[objective]) if objective else None
            ),
            "total_absolute_flux": float(sum(abs(value) for value in p_result.fluxes)),
            "cobra_pfba_objective_value": float(p_result.objective_value),
        },
    }


def run_mqc(
    model_path: str | Path,
    output_dir: str | Path,
    mqc_root: str | Path,
    *,
    python_executable: str | Path | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run the external MQC package and persist an auditable biomass readback.

    ``mqc_root`` must contain the checked-out package with ``mqc/main.py``.
    The input model is never modified; MQC writes its corrected model under
    ``output_dir``.  The caller may choose a Python environment containing
    MQC's dependencies (the default is the current interpreter).
    """
    source = Path(model_path).expanduser().resolve()
    root = Path(mqc_root).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if not source.is_file():
        raise ToolError(f"MQC model does not exist: {source}")
    if not (root / "mqc" / "main.py").is_file():
        raise ToolError(f"MQC package entry point is missing: {root / 'mqc/main.py'}")
    output.mkdir(parents=True, exist_ok=True)
    interpreter = str(python_executable or sys.executable)
    command = [
        interpreter,
        "-m",
        "mqc.main",
        "-m",
        str(source),
        "-o",
        str(output),
    ]
    environment = os.environ.copy()
    existing_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(root) + (os.pathsep + existing_path if existing_path else "")
    completed = subprocess.run(
        command,
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    log_path = output / "mqc-command.log"
    log_path.write_text(
        json.dumps(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise ToolError(
            f"MQC failed with exit code {completed.returncode}; see {log_path}"
        )
    corrected = output / "big_model.xml"
    if not corrected.is_file():
        raise ToolError(f"MQC completed without corrected model: {corrected}")
    check_path = output / "check_result.json"
    result = {
        "schema_version": "mqc-biomass-simulation.v1",
        "input_model": str(source),
        "input_model_sha256": _sha256(source),
        "mqc_root": str(root),
        "mqc_output_model": str(corrected),
        "mqc_output_model_sha256": _sha256(corrected),
        "mqc_check_result": str(check_path),
        "mqc_checks": _check_summary(check_path),
        "command_log": str(log_path),
        **_biomass_simulation(corrected),
    }
    summary_path = output / "mqc-biomass-simulation.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    result["summary_path"] = str(summary_path)
    return result


__all__ = ["run_mqc"]
