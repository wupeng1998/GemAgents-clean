"""Growth-condition contracts and public-reference calibration helpers."""

from __future__ import annotations

import json
import math
from html import unescape
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.contracts import load_configuration
from GemAgents.metabolic.library.source_io import load_source_model
from GemAgents.metabolic.media.selection import metabolic_set_medium
from GemAgents.metabolic.qc import Task


def reference_growth(config: dict) -> tuple[float, str] | None:
    """Evaluate the public reference under the reconstruction medium."""
    if not config.get("reference_growth_ceiling", False):
        return None
    source_path = Path(str(config.get("reference_support_path", "")))
    if not source_path.is_file():
        raise ToolError("reference_growth_ceiling requires reference_support_path")
    medium = config.get("medium", "minimal")
    if config.get("medium_file"):
        medium_path = Path(config["medium_file"])
        try:
            medium = load_configuration(medium_path)
        except (OSError, ValueError) as error:
            raise ToolError(f"Could not parse medium file: {medium_path}") from error
    reference = load_source_model(source_path)
    metabolic_set_medium(reference, medium)
    growth = reference.slim_optimize(error_value=None)
    if growth is None or not math.isfinite(float(growth)) or growth <= 0:
        raise ToolError("Reference model cannot grow in the selected medium")
    return float(growth), str(source_path)


def growth_tasks(model, biomass: str, minimum: float) -> tuple[Task, ...]:
    """Restore all reconstruction growth conditions as QC-preserved tasks."""
    tasks = [Task("declared_growth", biomass, minimum)]
    encoded = model.notes.get("growth_scenarios", "")
    if not encoded:
        return tuple(tasks)
    try:
        scenarios = json.loads(encoded)
    except (TypeError, ValueError) as error:
        # COBRApy's SBML notes reader can leave HTML entities (notably
        # ``&quot;``) in a string that was valid JSON before serialization.
        # Decode only on parse failure so literal entities inside valid
        # user-provided JSON remain untouched.
        decoded = unescape(encoded) if isinstance(encoded, str) else encoded
        if decoded == encoded:
            raise ToolError("Model growth_scenarios note is invalid JSON") from error
        try:
            scenarios = json.loads(decoded)
        except (TypeError, ValueError) as decoded_error:
            raise ToolError("Model growth_scenarios note is invalid JSON") from decoded_error
    if not isinstance(scenarios, list):
        raise ToolError("Model growth_scenarios note must contain a list")
    boundary_ids = {reaction.id for reaction in model.boundary}
    task_sources = [{"name": "declared_growth", "source": "declared_input"}]
    for scenario in scenarios:
        if not isinstance(scenario, dict) or scenario.get("name") == "declared_growth":
            continue
        scenario_medium = scenario.get("medium")
        if not isinstance(scenario_medium, dict):
            raise ToolError("Each growth scenario must contain a medium object")
        source = str(scenario.get("source", ""))
        evidence_role = str(scenario.get("evidence_role", ""))
        if source.startswith("public_reference") and evidence_role != (
            "prior_input_not_external_validation"
        ):
            raise ToolError("Reference-derived growth tasks must be declared as prior input")
        with model:
            model.medium = {
                reaction_id: float(rate)
                for reaction_id, rate in scenario_medium.items()
                if reaction_id in model.reactions
            }
            bounds = {
                reaction_id: model.reactions.get_by_id(reaction_id).bounds
                for reaction_id in boundary_ids
            }
        tasks.append(
            Task(
                str(scenario["name"]),
                biomass,
                float(scenario.get("minimum", minimum)),
                maximum=(
                    float(scenario["reference_growth"])
                    if scenario.get("reference_growth") is not None
                    and scenario.get("reference_growth_ceiling", True)
                    else None
                ),
                bounds=bounds,
            )
        )
        task_sources.append(
            {"name": str(scenario["name"]), "source": source, "evidence_role": evidence_role}
        )
    if len({task.name for task in tasks}) != len(tasks):
        raise ToolError("Growth scenario names must be unique")
    model.notes["growth_task_contract"] = json.dumps(task_sources, sort_keys=True)
    return tuple(tasks)
