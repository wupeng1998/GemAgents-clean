"""Quality-control execution and semantic artifact patch recording."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_quality_stage(
    *,
    config: dict,
    out: Path,
    manifest: dict,
    phase,
    model,
    biomass: str,
    gapfilled_artifact,
    artifact_store,
    quality_fn,
) -> dict[str, Any]:
    """Run deterministic QC and persist the repaired artifact plus patch."""
    phase("quality_control")
    before_quality = model.copy()
    model, quality = quality_fn(model, biomass, config)
    quality_attempt = artifact_store.begin_attempt("quality", gapfilled_artifact.artifact_id)
    quality_role = (
        "calibration" if quality.get("growth_calibration", {}).get("applied") else "raw"
    )
    repaired_artifact = artifact_store.write_model(
        model,
        "repaired",
        quality_role,
        parent_artifact_id=gapfilled_artifact.artifact_id,
        attempt=quality_attempt,
    )

    artifact_store.write_patch(
        quality_attempt,
        {
            "parent_artifact_id": gapfilled_artifact.artifact_id,
            "before_semantic_hash": gapfilled_artifact.semantic_hash,
            "after_semantic_hash": repaired_artifact.semantic_hash,
            "changes": _model_changes(before_quality, model),
            "tasks_before": quality.get("initial_tasks", []),
            "tasks_after": quality.get("final_tasks", []),
            "probes_before": (quality.get("rounds") or [[]])[0],
            "probes_after": quality.get("final_probes", []),
            "decision": "accepted" if quality.get("declared_checks_passed") else "rejected",
            "reason": quality.get("status", "quality status unavailable"),
        },
    )
    artifact_store.finish_attempt(
        quality_attempt,
        status="completed",
        gate_passed=bool(quality.get("declared_checks_passed")),
        artifact_ids=(repaired_artifact.artifact_id,),
        reason=quality.get("status"),
    )
    return {
        "model": model,
        "quality": quality,
        "current_artifact": repaired_artifact,
        "current_attempt": quality_attempt,
    }


def _model_changes(before, after) -> list[dict]:
    before_reactions = {reaction.id: reaction for reaction in before.reactions}
    after_reactions = {reaction.id: reaction for reaction in after.reactions}
    return [
        {
            "reaction_id": reaction_id,
            "before_bounds": (
                list(before_reactions[reaction_id].bounds)
                if reaction_id in before_reactions
                else None
            ),
            "after_bounds": (
                list(after_reactions[reaction_id].bounds)
                if reaction_id in after_reactions
                else None
            ),
        }
        for reaction_id in sorted(before_reactions.keys() | after_reactions.keys())
        if reaction_id not in before_reactions
        or reaction_id not in after_reactions
        or before_reactions[reaction_id].bounds != after_reactions[reaction_id].bounds
    ]
