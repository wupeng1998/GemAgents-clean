"""Immutable model stages and atomic accepted pointers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from GemAgents.metabolic.io.core import metabolic_hash
from GemAgents.metabolic.qc.certificate import semantic_model_hash, semantic_model_payload
from GemAgents.run_cache import atomic_json

STAGES = {"initial", "gapfilled", "repaired", "final"}
ROLES = {"raw", "calibration", "exploratory"}


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    path: Path
    kind: str
    parent_artifact_id: str | None


@dataclass(frozen=True)
class ModelArtifact:
    artifact_id: str
    stage: str
    role: str
    parent_artifact_id: str | None
    semantic_hash: str
    file_sha256: str
    model_path: str
    attempt_id: str | None


class ArtifactStore:
    def __init__(self, run_root: Path) -> None:
        self.root = run_root / "artifacts"
        self.stages = self.root / "stages"
        self.attempts = self.root / "attempts"

    def begin_attempt(self, kind: str, parent_artifact_id: str | None) -> Attempt:
        self.attempts.mkdir(parents=True, exist_ok=True)
        for number in range(1, 10000):
            attempt_id = f"{number:04d}"
            path = self.attempts / attempt_id
            try:
                path.mkdir()
            except FileExistsError:
                continue
            attempt = Attempt(attempt_id, path, kind, parent_artifact_id)
            atomic_json(
                path / "status.json",
                {
                    "attempt_id": attempt_id,
                    "kind": kind,
                    "parent_artifact_id": parent_artifact_id,
                    "status": "running",
                },
            )
            return attempt
        raise RuntimeError("No free attempt identifier")

    def finish_attempt(
        self,
        attempt: Attempt,
        *,
        status: str,
        gate_passed: bool,
        artifact_ids: tuple[str, ...] = (),
        reason: str | None = None,
    ) -> None:
        atomic_json(
            attempt.path / "status.json",
            {
                "attempt_id": attempt.attempt_id,
                "kind": attempt.kind,
                "parent_artifact_id": attempt.parent_artifact_id,
                "status": status,
                "gate_passed": gate_passed,
                "artifact_ids": list(artifact_ids),
                "reason": reason,
            },
        )

    def write_model(
        self,
        model,
        stage: str,
        role: str,
        *,
        parent_artifact_id: str | None = None,
        attempt: Attempt | None = None,
    ) -> ModelArtifact:
        if stage not in STAGES or role not in ROLES:
            raise ValueError("Unknown model stage or role")
        if stage == "final" and any(
            reaction.id.startswith("__probe_")
            or reaction.notes.get("audit_only") == "true"
            for reaction in model.reactions
        ):
            raise ValueError("Audit probe reactions cannot enter a final artifact")
        semantic_hash = semantic_model_hash(model)
        identity = {
            "stage": stage,
            "role": role,
            "parent_artifact_id": parent_artifact_id,
            "semantic_hash": semantic_hash,
            "attempt_id": attempt.attempt_id if attempt else None,
        }
        artifact_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        parent = self.stages / stage
        parent.mkdir(parents=True, exist_ok=True)
        destination = parent / artifact_id
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".artifact-", dir=parent))
            try:
                from cobra.io import read_sbml_model, write_sbml_model

                model_path = temporary / "model.xml"
                write_sbml_model(model, str(model_path))
                reloaded = read_sbml_model(str(model_path))
                if semantic_model_hash(reloaded) != semantic_hash:
                    expected = semantic_model_payload(model)
                    observed = semantic_model_payload(reloaded)
                    expected_reactions = {
                        row["id"]: row for row in expected["reactions"]
                    }
                    observed_reactions = {
                        row["id"]: row for row in observed["reactions"]
                    }
                    changed = []
                    for reaction_id in sorted(
                        set(expected_reactions) & set(observed_reactions)
                    ):
                        if expected_reactions[reaction_id] != observed_reactions[reaction_id]:
                            changed.append(
                                {
                                    "id": reaction_id,
                                    "expected": expected_reactions[reaction_id],
                                    "observed": observed_reactions[reaction_id],
                                }
                            )
                            if len(changed) >= 25:
                                break
                    atomic_json(
                        self.root.parent / f"semantic-mismatch-{stage}.json",
                        {
                            "stage": stage,
                            "expected_reaction_count": len(expected_reactions),
                            "observed_reaction_count": len(observed_reactions),
                            "missing_reactions": sorted(
                                set(expected_reactions) - set(observed_reactions)
                            )[:100],
                            "extra_reactions": sorted(
                                set(observed_reactions) - set(expected_reactions)
                            )[:100],
                            "changed_reactions": changed,
                            "expected_medium": expected["medium"],
                            "observed_medium": observed["medium"],
                        },
                    )
                    raise ValueError("SBML reload changed protected model semantics")
                record = ModelArtifact(
                    artifact_id,
                    stage,
                    role,
                    parent_artifact_id,
                    semantic_hash,
                    metabolic_hash(model_path),
                    str(destination / "model.xml"),
                    attempt.attempt_id if attempt else None,
                )
                atomic_json(temporary / "artifact.json", asdict(record))
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        payload = json.loads((destination / "artifact.json").read_text(encoding="utf-8"))
        return ModelArtifact(**payload)

    def import_model(
        self,
        path: Path,
        stage: str,
        role: str,
        *,
        parent_artifact_id: str | None = None,
        attempt: Attempt | None = None,
    ) -> ModelArtifact:
        from cobra.io import read_sbml_model

        return self.write_model(
            read_sbml_model(str(path)),
            stage,
            role,
            parent_artifact_id=parent_artifact_id,
            attempt=attempt,
        )

    def write_patch(self, attempt: Attempt, patch: dict[str, object]) -> Path:
        required = {
            "parent_artifact_id",
            "before_semantic_hash",
            "after_semantic_hash",
            "changes",
            "tasks_before",
            "tasks_after",
            "probes_before",
            "probes_after",
            "decision",
            "reason",
        }
        missing = sorted(required - patch.keys())
        if missing:
            raise ValueError(f"Model patch is missing fields: {missing}")
        encoded = json.dumps(patch, sort_keys=True, separators=(",", ":"))
        patch_id = hashlib.sha256(encoded.encode()).hexdigest()
        path = attempt.path / "patches" / f"{patch_id}.json"
        atomic_json(path, {"patch_id": patch_id, **patch})
        return path

    def promote(self, attempt: Attempt, artifact: ModelArtifact) -> None:
        status = json.loads((attempt.path / "status.json").read_text(encoding="utf-8"))
        if status.get("status") != "completed" or status.get("gate_passed") is not True:
            raise ValueError("Only a completed, gate-passing attempt can be accepted")
        previous = None
        pointer = self.root / "accepted.json"
        if pointer.is_file():
            previous = json.loads(pointer.read_text(encoding="utf-8")).get("artifact_id")
        atomic_json(
            pointer,
            {
                "artifact_id": artifact.artifact_id,
                "attempt_id": attempt.attempt_id,
                "previous_artifact_id": previous,
                "semantic_hash": artifact.semantic_hash,
                "file_sha256": artifact.file_sha256,
                "model_path": artifact.model_path,
            },
        )
