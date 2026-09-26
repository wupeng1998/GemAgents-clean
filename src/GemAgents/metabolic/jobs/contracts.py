"""Deterministic reconstruction contracts and routing metadata."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from GemAgents.checkpoint import request_hash
from GemAgents.contracts import (
    ReconstructionConfig,
    apply_reconstruction_defaults,
    default_biomass_library_path,
    default_reaction_library_path,
    load_configuration,
)
from GemAgents.errors import ToolError
from GemAgents.layout import RepoLayout
from GemAgents.metabolic.io.core import metabolic_hash
from GemAgents.run_cache import atomic_json, stable_key


class ContractError(ValueError):
    """A request cannot be safely compiled into a reconstruction job."""


@dataclass(frozen=True)
class ReconstructionContract:
    contract_id: str
    config_path: str
    input_path: str
    input_type: str
    organism_domain: str
    annotation_route: str
    evaluation_track: str
    medium: object
    biomass: str | None
    budgets: dict[str, object]
    required_qc: tuple[str, ...]
    external_access: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RouteDecision:
    status: str
    tool: str | None
    reason: str
    contract: ReconstructionContract | None = None
    config: dict[str, object] | None = None
    authorization: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON object without stringifying nested contracts."""
        return asdict(self)


@dataclass(frozen=True)
class IntentFrame:
    """Deterministic, reviewable interpretation of a natural-language request."""

    task_type: str
    input_path: str | None
    constraints: tuple[str, ...]
    medium: object | None
    biomass: str | None
    reference_policy: str
    budgets: dict[str, object]
    allowed_read: tuple[str, ...]
    allowed_write: tuple[str, ...]
    ambiguities: tuple[str, ...]
    constraint_fragments: tuple[str, ...] = ()
    execution_order: tuple[str, ...] = ()
    output_target: str | None = None
    authorization: dict[str, object] | None = None
    inspection_path: str | None = None
    reconstruction_input_path: str | None = None
    job_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    parameter_schema: dict[str, object]
    prerequisite_assets: tuple[str, ...]
    output_artifact_schema: dict[str, object]
    permissions: tuple[str, ...]
    side_effects: tuple[str, ...]
    budget: dict[str, object]
    failure_types: tuple[str, ...]
    version: str = "1.0"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key in ("prerequisite_assets", "permissions", "side_effects", "failure_types"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class ToolResult:
    """Stable result envelope for domain tool adapters."""

    status: str
    data: object = None
    artifacts: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    error: dict[str, object] | None = None
    retryable: bool = False
    cost: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["artifacts"] = list(self.artifacts)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload


def default_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            "reconstruction_route",
            {
                "type": "object",
                "required": ["request"],
                "properties": {"request": {"type": "string"}},
                "additionalProperties": False,
            },
            (),
            {"type": "object", "required": ["status", "reason"]},
            ("workspace_write",),
            ("create_contract",),
            {"max_seconds": 5},
            ("invalid_request", "blocked_asset", "unsupported"),
        ),
        ToolSpec(
            "metabolic_start",
            {
                "type": "object",
                "required": ["config_path"],
                "properties": {"config_path": {"type": "string"}},
                "additionalProperties": False,
            },
            ("input", "reaction_library", "biomass_library"),
            {"type": "object", "required": ["job_id", "contract_id", "status"]},
            ("workspace_write", "process_spawn"),
            ("create_job", "write_run"),
            {"max_seconds": 30, "cpus": "contract"},
            ("invalid_contract", "blocked_asset", "duplicate_job", "worker_failure"),
        ),
        ToolSpec(
            "metabolic_batch_start",
            {
                "type": "object",
                "required": [
                    "input_directory",
                    "output_directory",
                    "mapping",
                    "biomass_library",
                    "reaction_library",
                    "hmm_directory",
                ],
                "properties": {
                    "input_directory": {"type": "string"},
                    "output_directory": {"type": "string"},
                    "mapping": {"type": "string"},
                    "biomass_library": {"type": "string"},
                    "reaction_library": {"type": "string"},
                    "hmm_directory": {"type": "string"},
                },
                "additionalProperties": False,
            },
            ("input_directory", "mapping", "reaction_library", "biomass_library", "hmm_directory"),
            {"type": "object", "required": ["job_id", "status"]},
            ("workspace_write", "process_spawn"),
            ("create_batch_job", "write_batch_run"),
            {"max_seconds": 30, "cpus": "batch"},
            ("invalid_contract", "blocked_asset", "worker_failure"),
        ),
        ToolSpec(
            "metabolic_batch_status",
            {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
            (),
            {"type": "object", "required": ["job_id", "status"]},
            ("read_workspace",),
            (),
            {"max_seconds": 5},
            ("invalid_job", "missing_job"),
        ),
        ToolSpec(
            "metabolic_status",
            {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
            (),
            {"type": "object", "required": ["job_id", "status"]},
            ("read_workspace",),
            (),
            {"max_seconds": 5},
            ("invalid_job", "missing_job"),
        ),
        ToolSpec(
            "metabolic_resume",
            {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
            ("input", "checkpoint"),
            {"type": "object", "required": ["job_id", "status"]},
            ("workspace_write", "process_spawn"),
            ("update_job_ledger", "resume_worker"),
            {"max_seconds": 30},
            ("invalid_job", "missing_checkpoint", "stale_checkpoint", "worker_failure"),
        ),
        ToolSpec(
            "metabolic_cancel",
            {
                "type": "object",
                "required": ["job_id"],
                "properties": {
                    "job_id": {"type": "string"},
                    "terminate": {"type": "boolean", "default": False},
                    "grace_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                },
                "additionalProperties": False,
            },
            (),
            {"type": "object", "required": ["job_id", "status"]},
            ("workspace_write",),
            ("update_job_ledger",),
            {"max_seconds": 5},
            ("invalid_job", "missing_job"),
        ),
        ToolSpec(
            "model_inspect",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {"model_path": {"type": "string"}},
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "model_sha256"]},
            ("read_workspace",),
            (),
            {"max_seconds": 30},
            ("invalid_model", "missing_asset"),
        ),
        ToolSpec(
            "inspect_model_component",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "component": {
                        "type": "string",
                        "enum": [
                            "summary",
                            "reactions",
                            "metabolites",
                            "genes",
                            "exchanges",
                            "compartments",
                        ],
                    },
                    "identifier": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "operation", "model_sha256"]},
            ("read_workspace",),
            (),
            {"max_seconds": 30},
            ("invalid_model", "missing_asset", "invalid_component"),
        ),
        ToolSpec(
            "simulate_knockouts",
            {
                "type": "object",
                "required": ["model_path", "kind", "identifiers"],
                "properties": {
                    "model_path": {"type": "string"},
                    "kind": {"type": "string", "enum": ["gene", "reaction"]},
                    "identifiers": {"type": "array", "items": {"type": "string"}},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "operation", "model_sha256"]},
            ("read_workspace",),
            (),
            {"max_seconds": 240},
            ("invalid_model", "invalid_identifier", "solver_failure"),
        ),
        ToolSpec(
            "scan_essentiality",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "kind": {"type": "string", "enum": ["gene", "reaction"]},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 5000},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "operation", "model_sha256"]},
            ("read_workspace",),
            (),
            {"max_seconds": 600},
            ("invalid_model", "invalid_identifier", "solver_failure"),
        ),
        ToolSpec(
            "analyze_shadow_prices",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "operation", "model_sha256"]},
            ("read_workspace",),
            (),
            {"max_seconds": 120},
            ("invalid_model", "solver_failure"),
        ),
        ToolSpec(
            "simulate_fba",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                    "maintenance": {"type": "number"},
                    "maintenance_reaction_id": {"type": "string"},
                    "solver_tolerance": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "model_sha256", "solver_status"]},
            ("read_workspace",),
            (),
            {"max_seconds": 120},
            ("invalid_model", "infeasible", "unbounded", "solver_failure"),
        ),
        ToolSpec(
            "simulate_pfba",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                    "maintenance": {"type": "number"},
                    "maintenance_reaction_id": {"type": "string"},
                    "solver_tolerance": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "model_sha256", "solver_status"]},
            ("read_workspace",),
            (),
            {"max_seconds": 120},
            ("invalid_model", "infeasible", "unbounded", "solver_failure"),
        ),
        ToolSpec(
            "simulate_fva",
            {
                "type": "object",
                "required": ["model_path"],
                "properties": {
                    "model_path": {"type": "string"},
                    "medium": {"type": "object"},
                    "objective_id": {"type": "string"},
                    "maintenance": {"type": "number"},
                    "maintenance_reaction_id": {"type": "string"},
                    "solver_tolerance": {"type": "number", "exclusiveMinimum": 0},
                    "fraction_of_optimum": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                    },
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "model_sha256", "flux_ranges"]},
            ("read_workspace",),
            (),
            {"max_seconds": 120},
            ("invalid_model", "infeasible", "unbounded", "solver_failure"),
        ),
        ToolSpec(
            "compare_scenarios",
            {
                "type": "object",
                "required": ["model_path", "scenarios"],
                "properties": {
                    "model_path": {"type": "string"},
                    "scenarios": {"type": "object"},
                    "objective_id": {"type": "string"},
                    "maintenance": {"type": "number"},
                    "maintenance_reaction_id": {"type": "string"},
                    "solver_tolerance": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "model_sha256", "scenarios"]},
            ("read_workspace",),
            (),
            {"max_seconds": 240},
            ("invalid_model", "invalid_scenario", "solver_failure"),
        ),
        ToolSpec(
            "compare_models",
            {
                "type": "object",
                "required": ["models"],
                "properties": {"models": {"type": "object"}},
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["status", "models"]},
            ("read_workspace",),
            (),
            {"max_seconds": 120},
            ("invalid_model", "missing_asset"),
        ),
        ToolSpec(
            "render_report",
            {
                "type": "object",
                "required": ["result"],
                "properties": {"result": {"type": "object"}},
                "additionalProperties": False,
            },
            (),
            {"type": "object", "required": ["status", "claims", "limitations"]},
            ("read_workspace",),
            (),
            {"max_seconds": 10},
            ("invalid_result",),
        ),
        ToolSpec(
            "analyze_request",
            {
                "type": "object",
                "required": ["request", "model_path"],
                "properties": {
                    "request": {"type": "string"},
                    "model_path": {"type": "string"},
                    "scenarios": {"type": "object"},
                    "network_model_path": {"type": "string"},
                    "substrate_id": {"type": "string"},
                    "product_id": {"type": "string"},
                    "number_of_optimizations": {"type": "integer"},
                    "min_fraction": {"type": "number"},
                    "medium": {"type": "object"},
                    "maintenance": {"type": "number"},
                    "maintenance_reaction_id": {"type": "string"},
                    "solver_tolerance": {"type": "number", "exclusiveMinimum": 0},
                    "biomass_id": {"type": "string"},
                    "objective_id": {"type": "string"},
                    "steps": {"type": "integer"},
                    "use_fva": {"type": "boolean"},
                    "constrain_biomass": {"type": "boolean"},
                    "max_flux_cutoff": {"type": "number"},
                    "fraction_of_optimum": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                    },
                    "community_models": {"type": "object"},
                    "strain_design_type": {"type": "string"},
                    "strain_design_config": {"type": "object"},
                },
                "additionalProperties": False,
            },
            ("model",),
            {"type": "object", "required": ["contract", "result", "report"]},
            ("read_workspace",),
            (),
            {"max_seconds": 240},
            (
                "invalid_request",
                "invalid_model",
                "invalid_scenario",
                "infeasible",
                "solver_failure",
            ),
        ),
    )


def tool_spec_map() -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in default_tool_specs()}


def _validate_parameter_schema(value: object, schema: dict[str, object], path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ContractError(f"{path} must be an object")
        required = schema.get("required", ())
        missing = [key for key in required if key not in value]
        if missing:
            raise ContractError(f"{path} missing required fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ContractError(f"{path} has an invalid properties schema")
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties") is False:
            raise ContractError(f"{path} unknown fields: {', '.join(sorted(unknown))}")
        for key, rule in properties.items():
            if key in value and isinstance(rule, dict):
                _validate_parameter_schema(value[key], rule, f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ContractError(f"{path} must be an array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_parameter_schema(item, item_schema, f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise ContractError(f"{path} must be a string")
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ContractError(f"{path} must contain at least {minimum} characters")
    elif expected == "boolean" and type(value) is not bool:
        raise ContractError(f"{path} must be a boolean")
    elif expected == "integer":
        if type(value) is not int:
            raise ContractError(f"{path} must be an integer")
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ContractError(f"{path} must be >= {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ContractError(f"{path} must be <= {maximum}")
    elif expected == "number":
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ContractError(f"{path} must be a number (finite)")
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ContractError(f"{path} must be >= {minimum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            raise ContractError(f"{path} must be > {exclusive_minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ContractError(f"{path} must be <= {maximum}")
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise ContractError(f"{path} must be one of {allowed}")


def validate_tool_input(name: str, value: object) -> None:
    spec = tool_spec_map().get(name)
    if spec is None:
        return
    _validate_parameter_schema(value, spec.parameter_schema, name)


def validate_tool_output(name: str, value: object) -> None:
    """Validate a registered tool's structured output before it is audited."""
    spec = tool_spec_map().get(name)
    if spec is None:
        return
    _validate_parameter_schema(value, spec.output_artifact_schema, f"{name}.output")


def _find_batch_workbook(workspace: Path) -> Path | None:
    """Find the local benchmark workbook used for deterministic mapping."""
    preferred = workspace / "bigg模型比较_分析结果_new.xlsx"
    if preferred.is_file():
        return preferred
    for candidate in sorted(workspace.glob("*.xlsx")):
        try:
            from GemAgents.metabolic.jobs.bigg_inventory import read_inventory_rows

            read_inventory_rows(candidate)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
        return candidate
    return None


def _auto_prepare_batch_mapping(
    workspace: Path, sequence_directory: Path, output_path: Path
) -> tuple[Path | None, str | None]:
    """Prepare a versioned mapping from the supplied directory and local inventory."""
    preparation = output_path / "preparation"
    existing = sorted(preparation.glob("mapping.v2.*.json"), reverse=True)
    from GemAgents.batch_mapping import sha256_file, validate_prepared_mapping

    sequence_root = sequence_directory.resolve()

    def valid_mapping(candidate: Path) -> bool:
        if candidate.is_symlink():
            return False
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            builds = validate_prepared_mapping(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        observed = False
        for build in builds:
            sequence_value = build.get("sequence")
            if not sequence_value:
                continue
            sequence = Path(str(sequence_value))
            if not sequence.is_absolute():
                sequence = workspace / sequence
            try:
                sequence = sequence.resolve()
                sequence.relative_to(sequence_root)
            except ValueError:
                return False
            if not sequence.is_file():
                return False
            expected_hash = build.get("sequence_sha256")
            if expected_hash and sha256_file(sequence) != expected_hash:
                return False
            observed = True
        return observed

    if existing:
        for candidate in existing:
            if valid_mapping(candidate):
                return candidate, None

    if not sequence_directory.is_dir():
        return None, f"batch input directory does not exist: {sequence_directory}"
    workbook = _find_batch_workbook(workspace)
    if workbook is None:
        return None, "local benchmark workbook is missing"
    model_directory = workspace / "bigg_model_public"
    if not model_directory.is_dir() or not any(model_directory.glob("*.xml")):
        return None, f"public BiGG model XML directory is missing: {model_directory}"
    make_script = workspace / "scripts" / "make_bigg_explicit_mapping.py"
    prepare_script = workspace / "scripts" / "prepare_bigg_gemagents_batch.py"
    if not make_script.is_file() or not prepare_script.is_file():
        return None, "batch mapping preparation scripts are missing"

    try:
        preparation.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return None, f"batch mapping preparation directory is not writable: {error}"
    explicit = preparation / "mapping.explicit.auto.json"
    environment = dict(os.environ)
    source_root = str(workspace / "src")
    environment["PYTHONPATH"] = (
        source_root + os.pathsep + environment.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)

    def run(script: Path, arguments: list[str]) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                [sys.executable, str(script), *arguments],
                cwd=workspace,
                env=environment,
                text=True,
                capture_output=True,
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"{type(error).__name__}: {error}"
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            return False, detail[-1] if detail else f"{script.name} exited {completed.returncode}"
        return True, ""

    if not explicit.is_file():
        ok, detail = run(
            make_script,
            [
                "--workbook",
                str(workbook),
                "--sequence-directory",
                str(sequence_directory),
                "--model-directory",
                str(model_directory),
                "--output",
                str(explicit),
            ],
        )
        if not ok:
            return None, f"automatic batch mapping preparation failed: {detail}"

    before = set(preparation.glob("mapping.v2.*.json"))
    ok, detail = run(
        prepare_script,
        [
            "--workbook",
            str(workbook),
            "--sequence-directory",
            str(sequence_directory),
            "--model-directory",
            str(model_directory),
            "--output-directory",
            str(preparation),
            "--explicit-mapping",
            str(explicit),
        ],
    )
    if not ok:
        return None, f"automatic batch mapping preparation failed: {detail}"

    for candidate in sorted(preparation.glob("mapping.v2.*.json"), reverse=True):
        if candidate in before:
            continue
        if valid_mapping(candidate):
            return candidate, None
    return None, "automatic batch mapping completed without a valid versioned mapping"


def _resolve(workspace: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    resolved = (
        (workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    )
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError as error:
        raise ContractError(f"path escapes workspace: {value}") from error
    return resolved


def _latest_reconstruction_job_id(workspace: Path) -> str | None:
    """Return the most recently submitted persisted reconstruction job.

    Follow-up questions in the interactive session often omit the job ID (for
    example, ``建模结果如何``).  The job ledger is the durable source of truth
    across model turns and process restarts, so use its newest primary record
    as the implicit target.  Batch sidecars and malformed records are ignored.
    """
    jobs_root = RepoLayout(workspace).jobs
    if not jobs_root.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    for path in jobs_root.glob("*.json"):
        job_id = path.stem
        if not re.fullmatch(r"[a-f0-9]{32}", job_id, re.I):
            continue
        if path.name.endswith((".contract.json", ".config.json")):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            record_time = float(payload.get("submitted_unix", 0))
            if payload.get("job_id") != job_id:
                continue
            if payload.get("record_type", "metabolic_job") != "metabolic_job":
                continue
            if not isinstance(payload.get("status"), str):
                continue
            if record_time <= 0:
                record_time = path.stat().st_mtime
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        candidates.append((record_time, job_id.lower()))
    if not candidates:
        return None
    return max(candidates)[1]


def _input_type(path: Path, declared: str | None) -> str:
    if declared and declared != "auto":
        return declared
    # Keep routing and the execution pipeline on the same content-aware
    # detector.  In particular, the detector handles compressed FASTA and
    # does not infer type from a generic `.fa` suffix alone.
    if not path.is_file():
        raise ContractError(f"BLOCKED_ASSET: input does not exist: {path}")
    try:
        from GemAgents.metabolic.annotation import metabolic_detect_input

        kind, _reason = metabolic_detect_input(path, "auto")
    except (OSError, ValueError, ToolError) as error:
        raise ContractError(str(error)) from error
    if kind not in {"faa", "fna"}:
        raise ContractError("input_type must resolve to faa or fna")
    return kind


def _logical_suffix(path: str) -> str:
    """Return the content suffix while ignoring a case-insensitive gzip layer."""
    name = Path(path).name.casefold()
    if name.endswith(".gz"):
        name = name[:-3]
    return Path(name).suffix


_PROMPT_PATH_QUOTES = "\"'“”‘’`"


def _clean_prompt_path(value: str) -> str:
    """Normalize a path token extracted from natural language.

    Users commonly quote paths so that spaces are preserved.  The quote is a
    delimiter, not part of the filesystem name; keeping it in the token makes
    an existing directory appear to be missing (for example ``bigg\"``).
    """
    cleaned = value.strip()
    quote_pairs = (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"), ("`", "`"))
    for opening, closing in quote_pairs:
        if cleaned.startswith(opening) and cleaned.endswith(closing) and len(cleaned) >= 2:
            cleaned = cleaned[1:-1].strip()
            break
    cleaned = cleaned.strip(_PROMPT_PATH_QUOTES).strip()
    if cleaned not in {"/", "\\"} and not re.fullmatch(r"[A-Za-z]:[\\/]", cleaned):
        cleaned = cleaned.rstrip("/\\")
    return cleaned


def _extract_directory_paths(prompt: str) -> list[str]:
    """Extract directory paths before Chinese or English directory markers.

    A token-oriented expression cannot parse ``"/data/a folder"`` and may
    include the closing quote in ``"/data/bigg"文件夹``.  These expressions
    handle quoted paths first, then absolute and simple relative paths.
    """
    marker = r"(?:文件夹|目录)(?:下|中|内)?"
    patterns = (
        rf'"\s*([^"\r\n]+?)\s*"\s*{marker}',
        rf"'\s*([^'\r\n]+?)\s*'\s*{marker}",
        rf"“\s*([^”\r\n]+?)\s*”\s*{marker}",
        rf"‘\s*([^’\r\n]+?)\s*’\s*{marker}",
        rf"`\s*([^`\r\n]+?)\s*`\s*{marker}",
        rf"((?:/(?![\\/\s]|文件夹|目录)|[A-Za-z]:[\\/](?!\s))"
        rf"[^\"'“”‘’`\r\n,，。；;]*?)\s*{marker}",
        rf"([A-Za-z0-9_.~-][A-Za-z0-9_.~/-]*)\s*{marker}",
    )
    found: list[tuple[int, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, prompt, re.I):
            prefix = prompt[max(0, match.start() - 80) : match.start()]
            if re.search(
                r"(?:输出到|输出至|save\s+to|output\s+to)\s*[\"'“‘`]?\s*$",
                prefix,
                re.I,
            ):
                continue
            value = _clean_prompt_path(match.group(1))
            if value:
                found.append((match.start(), value))
    paths: list[str] = []
    for _, value in sorted(found):
        if value not in paths:
            paths.append(value)
    return paths


def _extract_output_path(prompt: str) -> str | None:
    """Extract the path following an output/save marker, including spaces."""
    labels = r"(?:输出到|输出至|save\s+to|output\s+to)"
    patterns = (
        rf'{labels}\s*"\s*([^"\r\n]+?)\s*"',
        rf"{labels}\s*'\s*([^'\r\n]+?)\s*'",
        rf"{labels}\s*“\s*([^”\r\n]+?)\s*”",
        rf"{labels}\s*‘\s*([^’\r\n]+?)\s*’",
        rf"{labels}\s*`\s*([^`\r\n]+?)\s*`",
        rf"{labels}\s*([^\s,，。；;]+)",
    )
    matches = [re.search(pattern, prompt, re.I) for pattern in patterns]
    matches = [match for match in matches if match is not None]
    if not matches:
        return None
    return _clean_prompt_path(matches[0].group(1)) or None


def compile_contract(
    config: dict[str, Any],
    workspace: Path,
    *,
    config_path: Path | None = None,
    require_assets: bool = True,
) -> ReconstructionContract:
    # Keep ambiguous EC matches as explicit OR-GPR candidates by default.
    # Strict mapping remains available with allow_ambiguous_ec_gpr=false.
    effective_config = apply_reconstruction_defaults(config, workspace)
    effective_config.setdefault("allow_ambiguous_ec_gpr", True)
    ReconstructionConfig(effective_config)
    config = effective_config
    if not isinstance(config.get("input"), str):
        raise ContractError("Reconstruction config requires an input path")
    input_path = _resolve(workspace, config["input"])
    input_type = _input_type(input_path, config.get("input_type"))
    annotation = config.get("annotation", "auto")
    if annotation == "auto":
        annotation = "pgap" if input_type == "fna" else "ncbi-hmm"
    if input_type == "faa" and annotation == "pgap":
        raise ContractError("FAA inputs cannot route to full PGAP")
    if input_type == "fna" and annotation == "ncbi-hmm":
        raise ContractError("raw FNA requires PGAP or explicit pyrodigal-ncbi-hmm")
    if require_assets and not input_path.is_file():
        raise ContractError(f"BLOCKED_ASSET: input does not exist: {input_path}")

    config_paths = {}
    for key in (
        "annotation_gbk",
        "pgap_output",
        "pgap_script",
        "pgap_config",
        "reaction_library",
        "biomass_library",
        "hmm_dir",
        "reference_support_path",
        "reference_protein_fasta",
    ):
        if config.get(key):
            path = _resolve(workspace, str(config[key]))
            config_paths[key] = str(path)
            if require_assets and not path.exists():
                raise ContractError(f"BLOCKED_ASSET: {key} does not exist: {path}")

    # CLEAN is an optional external checkout and is commonly installed beside
    # the GemAgents workspace.  It is still validated as an existing asset,
    # but it is not constrained by the workspace-only input path rule.
    for key in ("clean_runtime", "clean_python"):
        if config.get(key):
            path = Path(str(config[key])).expanduser().resolve()
            config_paths[key] = str(path)
            if require_assets and not path.exists():
                raise ContractError(f"BLOCKED_ASSET: {key} does not exist: {path}")

    if annotation == "pgap" and require_assets:
        runtime = RepoLayout(workspace).assets / "pgap" / "runtime.json"
        if not config_paths.get("pgap_script") and not runtime.is_file():
            raise ContractError("BLOCKED_ASSET: PGAP runtime or pgap_script is missing")
    if annotation in {"ncbi-hmm", "pyrodigal-ncbi-hmm"} and require_assets:
        hmm_dir = RepoLayout(workspace).assets / "ncbi_hmm"
        if not config_paths.get("hmm_dir") and not hmm_dir.is_dir():
            raise ContractError("BLOCKED_ASSET: NCBI HMM directory is missing")

    normalized = dict(config)
    normalized["input"] = str(input_path)
    normalized["input_type"] = input_type
    normalized["annotation"] = annotation
    normalized.update(config_paths)
    identity = {
        "config": normalized,
        "input_sha256": metabolic_hash(input_path) if input_path.is_file() else None,
    }
    contract_id = stable_key(identity)
    required_qc = ["quality_profile"]
    if config.get("memote", True):
        required_qc.append("memote")
    return ReconstructionContract(
        contract_id=contract_id,
        config_path=str(config_path.resolve()) if config_path else "",
        input_path=str(input_path),
        input_type=input_type,
        organism_domain=str(config.get("kingdom", "bacteria")),
        annotation_route=annotation,
        evaluation_track=str(config.get("evaluation_track", "unclassified")),
        medium=config.get("medium", "minimal"),
        biomass=config.get("biomass_template"),
        budgets={
            key: config[key]
            for key in ("cpus", "timeout", "solver_timeout", "max_edits")
            if key in config
        },
        required_qc=tuple(required_qc),
        external_access=tuple(
            key
            for key in ("reference_support_path", "reference_protein_fasta", "annotation_gbk")
            if key in config_paths
        ),
    )


def compile_config_file(path: Path, workspace: Path) -> ReconstructionContract:
    config = load_configuration(path)
    return compile_contract(config, workspace, config_path=path)


def _materialize_route_config(
    workspace: Path, contract: ReconstructionContract, config: dict[str, object]
) -> Path:
    """Create the immutable, validated config artifact used by a ready route."""
    layout = RepoLayout(workspace)
    path = layout.writable(f".gemagents/contracts/{contract.contract_id}.json")
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ContractError(f"prepared contract config is unreadable: {path}") from error
        if existing != config:
            # Relative and absolute spellings can describe the same validated
            # contract (for example ``Escherichi.faa`` versus its workspace
            # absolute path).  Revalidate the immutable file and accept it
            # when its canonical contract identity is unchanged; still reject
            # a substantive edit under an existing contract id.
            try:
                existing_contract = compile_config_file(path, workspace)
            except (ContractError, ValueError, OSError) as error:
                raise ContractError(
                    "prepared contract config changed; refusing overwrite"
                ) from error
            if existing_contract.contract_id != contract.contract_id:
                raise ContractError("prepared contract config changed; refusing overwrite")
    else:
        atomic_json(path, config)
    try:
        verified = compile_config_file(path, workspace)
    except (ContractError, ValueError, OSError) as error:
        raise ContractError(f"prepared contract config failed validation: {path}") from error
    if verified.contract_id != contract.contract_id:
        raise ContractError("prepared contract config identity differs from route contract")
    return path


def route_request(
    prompt: str, workspace: Path, *, model: str | None = None
) -> RouteDecision:
    """Route a request without an LLM and prepare deterministic batch inputs."""
    intent = compile_intent_frame(prompt, workspace)
    if intent.task_type in {"inspect", "read_only"}:
        authorization = dict(intent.authorization or {})
        if intent.inspection_path is not None:
            authorization.update(
                {
                    "status": "model_inspection_request",
                    "request_hash": request_hash(prompt, model or ""),
                    "task_type": intent.task_type,
                    "input_path": intent.inspection_path,
                    "execution_order": list(intent.execution_order),
                }
            )
            reason = "existing SBML/XML model inspection is read-only"
        else:
            reason = "request is an inspection task"
        return RouteDecision(
            "read_only",
            None,
            reason,
            authorization=authorization,
        )
    if intent.task_type == "batch_reconstruct":
        input_path = intent.input_path
        if input_path is None:
            return RouteDecision(
                "needs_input",
                None,
                "batch reconstruction input directory is missing",
                authorization=intent.authorization,
            )
        try:
            directory = _resolve(workspace, input_path)
        except ContractError as error:
            return RouteDecision(
                "blocked_asset", None, str(error), authorization=intent.authorization
            )
        if directory.is_symlink() or not directory.is_dir():
            return RouteDecision(
                "blocked_asset",
                None,
                f"BLOCKED_ASSET: batch input directory does not exist: {directory}",
                authorization=intent.authorization,
            )
        output = intent.output_target or "runs/batch"
        try:
            output_path = _resolve(workspace, output)
        except ContractError as error:
            return RouteDecision(
                "blocked_asset", None, str(error), authorization=intent.authorization
            )
        mapping_candidates = [
            *sorted(
                (output_path / "preparation").glob("mapping.repaired_native_*.json"),
                reverse=True,
            ),
            *sorted(
                (output_path / "preparation").glob("mapping.v2.*.json"),
                reverse=True,
            ),
            output_path / "preparation" / "mapping.native.json",
            output_path / "preparation" / "mapping.json",
            *sorted(
                (workspace / "bigg_model" / "preparation").glob(
                    "mapping.repaired_native_*.json"
                ),
                reverse=True,
            ),
            workspace / "bigg_model" / "preparation" / "mapping.native.json",
            workspace / "bigg_model" / "preparation" / "mapping.json",
        ]
        mapping_path = next(
            (candidate for candidate in mapping_candidates if candidate.is_file()), None
        )
        if mapping_path is None:
            mapping_path, mapping_error = _auto_prepare_batch_mapping(
                workspace, directory, output_path
            )
            if mapping_path is None:
                return RouteDecision(
                    "blocked_asset",
                    None,
                    f"BLOCKED_ASSET: automatic batch mapping is unavailable: {mapping_error}",
                    authorization=intent.authorization,
                )
        biomass_candidates = (
            output_path / "biomass_library",
            workspace / "bigg_model" / "biomass_library",
            workspace / default_biomass_library_path(workspace),
            workspace / "data" / "prokaryotic_biomass_library",
        )
        biomass_path = next(
            (
                path
                for path in biomass_candidates
                if path.is_dir() and (path / "catalog.json").is_file()
            ),
            None,
        )
        asset_paths = {
            "biomass_library": biomass_path,
            "reaction_library": workspace / default_reaction_library_path(workspace),
            "hmm_directory": workspace / "data" / "ncbi_hmm",
        }
        missing = [
            name
            for name, path in asset_paths.items()
            if path is None or not path.is_dir()
        ]
        if missing:
            return RouteDecision(
                "blocked_asset",
                None,
                f"BLOCKED_ASSET: batch assets are missing: {', '.join(missing)}",
                authorization=intent.authorization,
            )
        authorization = dict(intent.authorization or {})
        authorization.update(
            {
                "status": "batch_request_declared",
                "request_hash": request_hash(prompt, model or ""),
                "task_type": intent.task_type,
                "input_directory": input_path,
                "output_target": str(output_path),
                "mapping": str(mapping_path),
                **{name: str(path) for name, path in asset_paths.items() if path is not None},
                "execution_order": list(intent.execution_order),
            }
        )
        return RouteDecision(
            "ready",
            "metabolic_batch_start",
            (
                "directory batch reconstruction is validated; submit the resumable batch "
                f"worker for {input_path}"
            ),
            authorization=authorization,
        )
    if intent.task_type == "status":
        if intent.ambiguities:
            return RouteDecision(
                "needs_input",
                None,
                "; ".join(intent.ambiguities),
                authorization=intent.authorization,
            )
        authorization = dict(intent.authorization or {})
        authorization.update(
            {
                "status": "job_status_request",
                "request_hash": request_hash(prompt, model or ""),
                "task_type": intent.task_type,
                "job_id": intent.job_id,
            }
        )
        return RouteDecision(
            "ready",
            "metabolic_status",
            "metabolic job status request validated",
            authorization=authorization,
        )
    if intent.ambiguities:
        if any("eukaryotic" in item for item in intent.ambiguities):
            return RouteDecision(
                "blocked_unsupported",
                None,
                "; ".join(intent.ambiguities),
                authorization=intent.authorization,
            )
        return RouteDecision(
            "needs_input",
            None,
            "; ".join(intent.ambiguities),
            authorization=intent.authorization,
        )
    if intent.task_type in {"cancel", "resume"}:
        authorization = dict(intent.authorization or {})
        authorization.update(
            {
                "status": "job_control_request",
                "request_hash": request_hash(prompt, model or ""),
                "task_type": intent.task_type,
                "job_id": intent.job_id,
            }
        )
        if intent.task_type == "cancel":
            return RouteDecision(
                "ready",
                "metabolic_cancel",
                "cooperative cancellation request validated",
                authorization=authorization,
            )
        return RouteDecision(
            "ready",
            "metabolic_resume",
            "verified checkpoint resume request validated; job content is checked before spawn",
            authorization=authorization,
        )
    if intent.task_type in {"analyze", "compare", "report"}:
        input_path = intent.input_path
        if input_path is None:
            return RouteDecision(
                "needs_input",
                None,
                "analysis model path is missing",
                authorization=intent.authorization,
            )
        try:
            model_path = _resolve(workspace, input_path)
        except ContractError as error:
            return RouteDecision(
                "blocked_asset", None, str(error), authorization=intent.authorization
            )
        if not model_path.is_file():
            return RouteDecision(
                "blocked_asset",
                None,
                f"BLOCKED_ASSET: analysis model does not exist: {model_path}",
                authorization=intent.authorization,
            )
        authorization = dict(intent.authorization or {})
        authorization.update(
            {
                "status": "read_only_request",
                "request_hash": request_hash(prompt, model or ""),
                "task_type": intent.task_type,
                "input_path": input_path,
                "execution_order": list(intent.execution_order),
            }
        )
        return RouteDecision(
            "read_only",
            None,
            "analysis request is read-only",
            authorization=authorization,
        )
    unsupported_constraints = set(intent.constraints) & {
        "inspect_before_reconstruct", "biomass_unchanged"
    }
    if "medium_unchanged" in intent.constraints and intent.medium is None:
        return RouteDecision(
            "needs_input",
            None,
            "medium_unchanged requires an explicit baseline medium",
            authorization=intent.authorization,
        )
    if "biomass_unchanged" in unsupported_constraints:
        return RouteDecision(
            "needs_input",
            None,
            "biomass_unchanged requires an explicit baseline biomass identity",
            authorization=intent.authorization,
        )
    if "inspect_before_reconstruct" in unsupported_constraints:
        authorization = dict(intent.authorization or {})
        authorization.update(
            {
                "status": "approval_required",
                "request_hash": request_hash(prompt, model or ""),
                "approval_required": True,
                "approval_reason": "inspection must complete before reconstruction",
                "inspection_path": intent.inspection_path,
                "reconstruction_input_path": intent.reconstruction_input_path or intent.input_path,
                "plan": [
                    {"operation": "inspect", "input_path": intent.inspection_path},
                    {
                        "operation": "reconstruct",
                        "input_path": intent.reconstruction_input_path or intent.input_path,
                        "requires_approval": True,
                    },
                ],
                "execution_order": list(intent.execution_order),
            }
        )
        return RouteDecision(
            "needs_approval",
            None,
            "inspection must complete before reconstruction approval",
            authorization=authorization,
        )
    if unsupported_constraints:
        return RouteDecision(
            "needs_input",
            None,
            "request constraints need an explicit inspected model and approved execution order: "
            + ", ".join(sorted(unsupported_constraints)),
            authorization=intent.authorization,
        )
    if intent.task_type != "reconstruct":
        return RouteDecision(
            "blocked_missing_input",
            None,
            "no reconstruction task was declared",
            authorization=intent.authorization,
        )
    input_path = intent.reconstruction_input_path or intent.input_path
    assert input_path is not None
    try:
        detected_type = _input_type(_resolve(workspace, input_path), "auto")
    except ContractError as error:
        return RouteDecision(
            "blocked_asset",
            None,
            str(error),
            authorization=intent.authorization,
        )
    config = {
        "input": input_path,
        "input_type": "auto",
        "engine": "native",
        "allow_ambiguous_ec_gpr": True,
        "annotation": "ncbi-hmm"
        if detected_type == "faa"
        else "pgap",
    }
    if intent.medium is not None:
        config["medium"] = intent.medium
    if intent.biomass is not None:
        config["biomass_template"] = intent.biomass
    if intent.output_target is not None:
        config["output"] = intent.output_target
    if intent.budgets:
        config.update(intent.budgets)
    if intent.reference_policy == "forbidden":
        config["reference_support"] = False
    config = apply_reconstruction_defaults(config, workspace)
    try:
        contract = compile_contract(config, workspace)
    except ContractError as error:
        return RouteDecision("blocked_asset", None, str(error))
    try:
        config_path = _materialize_route_config(workspace, contract, config)
        contract = compile_contract(config, workspace, config_path=config_path)
    except ContractError as error:
        return RouteDecision(
            "blocked_contract", None, str(error), authorization=intent.authorization
        )
    authorization = dict(intent.authorization or {})
    authorization.update(
        {
            "status": "request_declared",
            "request_hash": request_hash(prompt, model or ""),
            "contract_id": contract.contract_id,
            "task_type": intent.task_type,
            "constraints": list(intent.constraints),
            "constraint_fragments": list(intent.constraint_fragments),
            "execution_order": list(intent.execution_order),
            "input_path": intent.input_path,
            "reconstruction_input_path": intent.reconstruction_input_path,
            "inspection_path": intent.inspection_path,
            "output_target": intent.output_target,
            "medium": intent.medium,
            "biomass": intent.biomass,
            "budgets": dict(intent.budgets),
        }
    )
    return RouteDecision(
        "ready",
        "metabolic_start",
        "deterministic contract validated",
        contract,
        config,
        authorization,
    )


def compile_intent_frame(prompt: str, workspace: Path) -> IntentFrame:
    """Extract only deterministic fields; unresolved semantics remain ambiguities."""
    text = prompt.casefold()
    reconstruct_words = (
        "reconstruct",
        "rebuild",
        "build",
        "重建",
        "补洞",
        "构建",
        "建立",
        "创建",
        "生成",
        "建模",
        "代谢网络",
    )
    batch_words = (
        "all sequences",
        "every sequence",
        "all strains",
        "every strain",
        "all genomes",
        "batch",
        "所有序列",
        "全部序列",
        "每个序列",
        "所有菌株",
        "全部菌株",
        "每个菌株",
        "所有菌种",
        "全部菌种",
        # Chinese users commonly describe the contents of a directory with
        # ``文件夹内/目录内`` or ``文件夹中/目录中``.  These phrases carry
        # the same batch intent as ``文件夹下``; without them the extracted
        # directory is incorrectly routed through the single-file detector.
        "文件夹内",
        "目录内",
        "文件夹中",
        "目录中",
        "批量",
        "目录下",
        "文件夹下",
        "directory",
        "folder",
    )
    analysis_words = ("analyze", "analysis", "fba", "pfba", "fva", "分析")
    compare_words = ("compare", "比较", "对比")
    report_words = ("report", "报告", "汇报")
    cancel_words = ("cancel", "取消", "停止")
    resume_words = ("resume", "恢复", "续跑")
    status_words = ("status", "状态", "进度", "poll", "查询任务", "查看任务")
    # Conversational follow-ups commonly omit both the literal word
    # ``status`` and the job ID.  They still refer to the previously submitted
    # reconstruction, so classify these phrases as status requests when no
    # explicit asset path is present (the path check is repeated below after
    # the deterministic path extraction).
    followup_status_phrases = (
        "建模结果",
        "模型结果",
        "任务结果",
        "结果如何",
        "结果怎么样",
        "运行情况",
        "当前情况",
        "进行到哪",
        "做到哪",
        "跑到哪",
        "完成了吗",
        "完成了么",
        "结束了吗",
        "任务怎么样",
        "how is it going",
        "is it done",
        "done yet",
    )
    job_ids = re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", text, re.I)
    explicit_asset_suffix = re.search(
        r"\.(?:faa|pep|fna|ffn|fasta|fa|xml|sbml)(?:\.gz)?", text, re.I
    )
    job_status_request = (
        len(job_ids) == 1 and any(word in text for word in status_words)
    ) or (
        explicit_asset_suffix is None
        and (
            any(word in text for word in status_words)
            or any(phrase in text for phrase in followup_status_phrases)
        )
    )
    negative_rebuild = bool(
        re.search(
            r"(?:请勿|不要|禁止|无需|无须|不用|不需要|不必|不)\s*"
            r"(?:进行|执行)?\s*(?:重建|rebuild|reconstruct)"
            r"|(?:do not|don't|no)\s+(?:rebuild|reconstruct(?:ion)?)"
            r"|without\s+(?:rebuilding|reconstruction)",
            text,
            re.I,
        )
    )
    if any(word in text for word in cancel_words):
        task_type = "cancel"
    elif any(word in text for word in resume_words):
        task_type = "resume"
    elif job_status_request:
        task_type = "status"
    elif any(word in text for word in compare_words):
        task_type = "compare"
    elif any(word in text for word in analysis_words):
        task_type = "analyze"
    elif any(word in text for word in report_words):
        task_type = "report"
    elif any(word in text for word in reconstruct_words):
        task_type = "reconstruct"
    else:
        task_type = "inspect"
    if negative_rebuild and task_type == "reconstruct":
        task_type = "read_only"
    raw_task_type = task_type
    # Keep `inspect` as the IntentFrame task type.  The route decision maps it
    # to the read-only tool status without losing the user's requested task.
    path_pattern = (
        r"[\"']([^\"']+\.(?:faa|pep|fna|ffn|fasta|fa|xml|sbml)(?:\.gz)?)[\"']|"
        # Chinese particles such as "为" or "从" commonly touch an
        # unquoted filename.  Treat CJK characters as a delimiter so they do
        # not become part of the path before workspace validation.  Absolute
        # paths need a separate branch: a perfectly valid path can contain
        # spaces (for example ``/data/Acinetobacter baumannii.faa``), and the
        # old token-oriented branch would skip the directory and restart at
        # the final basename, silently resolving it in the workspace root.
        r"(?:^|[\s\u3400-\u9fff:：])((?:[A-Za-z]:[\\/]|/)[^,;。；\u3400-\u9fff]*"
        r"\.(?:faa|pep|fna|ffn|fasta|fa|xml|sbml)(?:\.gz)?)|"
        r"(?:^|[\s\u3400-\u9fff:：])((?:[A-Za-z0-9_.-])[^ ,;。；\u3400-\u9fff]*"
        r"\.(?:faa|pep|fna|ffn|fasta|fa|xml|sbml)(?:\.gz)?)"
    )
    paths = [
        _clean_prompt_path(next(value for value in match if value))
        for match in re.findall(path_pattern, prompt, re.I)
    ]
    # Directory requests do not have a file suffix to anchor the original
    # sequence/model path expression.  Keep this extraction deliberately
    # conservative: only paths explicitly followed by a directory marker are
    # treated as batch inputs, and the marker itself is removed before
    # workspace validation.
    directory_paths = _extract_directory_paths(prompt)
    batch_input_path = directory_paths[0] if directory_paths else None
    input_path = paths[0] if paths else None
    model_paths = [
        path for path in paths if _logical_suffix(path) in {".xml", ".sbml"}
    ]
    sequence_paths = [
        path
        for path in paths
        if _logical_suffix(path) in {
            ".faa", ".pep", ".fna", ".ffn", ".fasta", ".fa"
        }
    ]
    inspection_path = model_paths[0] if model_paths else None
    reconstruction_input_path = sequence_paths[-1] if sequence_paths else None
    # Match intent words against the prose, not against extracted asset paths.
    # A model at ``/work/build/reconstruct/model.xml`` is still an inspection
    # input; path components such as ``build`` must not become a reconstruction
    # command.  Keep the original prompt for path values and constraint
    # fragments so the returned evidence remains faithful to the request.
    intent_text = text
    for path in (*paths, *directory_paths):
        token = path.casefold()
        if token:
            intent_text = intent_text.replace(token, " ")

    quoted_command_hint = (
        any(marker in text for marker in ("引用", "示例", "example", "quoted"))
        and any(
            marker in text
            for marker in (
                reconstruct_words
                + analysis_words
                + compare_words
                + report_words
                + cancel_words
                + resume_words
            )
        )
    )

    job_status_request = (
        len(job_ids) == 1 and any(word in intent_text for word in status_words)
    ) or (
        explicit_asset_suffix is None
        and (
            any(word in intent_text for word in status_words)
            or any(phrase in intent_text for phrase in followup_status_phrases)
        )
    )
    negative_rebuild = bool(
        re.search(
            r"(?:请勿|不要|禁止|无需|无须|不用|不需要|不必|不)\s*"
            r"(?:进行|执行)?\s*(?:重建|rebuild|reconstruct)"
            r"|(?:do not|don't|no)\s+(?:rebuild|reconstruct(?:ion)?)"
            r"|without\s+(?:rebuilding|reconstruction)",
            intent_text,
            re.I,
        )
    )
    if any(word in intent_text for word in cancel_words):
        task_type = "cancel"
    elif any(word in intent_text for word in resume_words):
        task_type = "resume"
    elif job_status_request:
        task_type = "status"
    elif any(word in intent_text for word in compare_words):
        task_type = "compare"
    elif any(word in intent_text for word in analysis_words):
        task_type = "analyze"
    elif any(word in intent_text for word in report_words):
        task_type = "report"
    elif any(word in intent_text for word in reconstruct_words):
        task_type = "reconstruct"
    else:
        task_type = "inspect"
    if negative_rebuild and task_type == "reconstruct":
        task_type = "read_only"
    # Keep the command kind visible long enough for the non-executable quoted
    # command guard below.  Its path token may have been masked as data, while
    # the quoted command still needs to produce ``needs_input`` rather than a
    # generic read-only inspection route.
    if quoted_command_hint:
        task_type = raw_task_type

    job_id = job_ids[0].lower() if len(job_ids) == 1 else None
    if task_type == "status" and job_id is None:
        job_id = _latest_reconstruction_job_id(workspace)
    if task_type in {"analyze", "compare", "report", "inspect", "read_only"}:
        input_path = model_paths[0] if model_paths else input_path
    elif reconstruction_input_path is not None:
        input_path = reconstruction_input_path
    if task_type == "reconstruct" and batch_input_path is not None:
        input_path = batch_input_path
        if any(word in intent_text for word in batch_words):
            task_type = "batch_reconstruct"
    constraints = []
    if negative_rebuild:
        constraints.append("no_reconstruction")
    if ("先检查" in prompt and "再重建" in prompt) or "inspect before rebuild" in intent_text:
        constraints.append("inspect_before_reconstruct")
    if (
        any(marker in intent_text for marker in ("如果", "若", "当", "if ", "when "))
        and any(marker in intent_text for marker in ("检查", "inspect", "audit"))
        and any(marker in intent_text for marker in reconstruct_words)
        and "inspect_before_reconstruct" not in constraints
    ):
        constraints.append("inspect_before_reconstruct")
    if any(
        word in intent_text
        for word in (
            "不得改变 biomass",
            "不要改变 biomass",
            "不得改变 生物量",
            "不要改变 生物量",
            "do not change biomass",
        )
    ):
        constraints.append("biomass_unchanged")
    if any(
        marker in intent_text
        for marker in (
            "不能修改原模型",
            "不要修改原模型",
            "不修改原模型",
            "不要改原模型",
            "do not modify the original model",
            "do not modify model",
            "keep the original model unchanged",
        )
    ):
        constraints.append("model_unchanged")
    if any(
        marker in intent_text
        for marker in (
            "不得改变培养基",
            "不要改变培养基",
            "不改变培养基",
            "do not change medium",
            "keep the medium unchanged",
        )
    ):
        constraints.append("medium_unchanged")
    if (
        "禁止 reference" in intent_text
        or "不要使用 reference" in intent_text
        or "no reference" in intent_text
    ):
        constraints.append("no_reference_support")
    quoted_command = (
        # Quoted commands are deliberately checked against the original
        # prompt: the path masker may remove a quoted ``reconstruct foo.faa``
        # token, but that token is still a non-executable command example.
        quoted_command_hint
    )
    def named_value(labels: tuple[str, ...]) -> str | None:
        label = "|".join(re.escape(item) for item in labels)
        match = re.search(
            rf"(?:{label})\s*(?:=|:|为|是)\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s,;，、。；：！？]+))",
            prompt,
            re.I,
        )
        if not match:
            return None
        return next((value for value in match.groups() if value is not None), None)

    medium = named_value(("medium", "培养基"))
    biomass = named_value(("biomass", "生物量"))
    budgets: dict[str, object] = {}
    budget_aliases = {
        "cpus": ("cpus", "cpu", "并行数"),
        "timeout": ("timeout", "超时"),
        "solver_timeout": ("solver_timeout", "求解器超时"),
        "max_edits": ("max_edits", "最大编辑数"),
    }
    budget_ambiguities = []
    for field, labels in budget_aliases.items():
        value = named_value(labels)
        if value is None:
            continue
        if not value.isdigit():
            budget_ambiguities.append(f"{field} must be a non-negative integer")
            continue
        parsed = int(value)
        if field != "max_edits" and parsed < 1:
            budget_ambiguities.append(f"{field} must be at least one")
            continue
        if field == "cpus" and parsed > 128:
            budget_ambiguities.append("cpus must be <= 128")
            continue
        budgets[field] = parsed

    ambiguities = []
    if task_type in {"reconstruct", "batch_reconstruct"} and input_path is None:
        ambiguities.append("reconstruction input path is missing")
    if task_type in {"cancel", "resume", "status"}:
        if not job_id:
            ambiguities.append(f"{task_type} job ID is missing")
        elif len(job_ids) > 1:
            ambiguities.append(f"{task_type} request names multiple job IDs")
    if task_type in {"analyze", "compare", "report"} and input_path is None:
        ambiguities.append("analysis model path is missing")
    if "真核" in intent_text or "euk" in intent_text:
        ambiguities.append("eukaryotic frontend is unsupported")
    if quoted_command:
        ambiguities.append("quoted command is not an execution request")
    if medium is not None and medium not in {"minimal", "glucose_minimal", "rich"}:
        ambiguities.append("medium must be minimal, glucose_minimal, or rich")
    ambiguities.extend(budget_ambiguities)
    fragments: list[str] = []
    fragment_patterns = (
        (
            "no_reconstruction",
            r"(?:请勿|不要|禁止|无需|无须|不用|不需要|不必|不)\s*"
            r"(?:进行|执行)?\s*(?:重建|rebuild|reconstruct)|"
            r"(?:do not|don't|no)\s+(?:rebuild|reconstruct(?:ion)?)|"
            r"without\s+(?:rebuilding|reconstruction)",
        ),
        ("inspect_before_reconstruct", r"(?:先检查.*?再重建|inspect before rebuild)"),
        (
            "biomass_unchanged",
            r"(?:不得改变 biomass|不要改变 biomass|不得改变 生物量|不要改变 生物量|"
            r"do not change biomass)",
        ),
        (
            "model_unchanged",
            r"(?:不能修改原模型|不要修改原模型|不修改原模型|不要改原模型|"
            r"do not modify the original model|do not modify model|"
            r"keep the original model unchanged)",
        ),
        (
            "medium_unchanged",
            r"(?:不得改变培养基|不要改变培养基|不改变培养基|do not change medium|"
            r"keep the medium unchanged)",
        ),
        (
            "no_reference_support",
            r"(?:禁止 reference[^。；;，,]*|不要使用 reference[^。；;，,]*|"
            r"no reference[^。；;，,]*)",
        ),
    )
    for _label, pattern in fragment_patterns:
        match = re.search(pattern, prompt, re.I)
        if match:
            fragments.append(match.group(0).strip())

    output_target = named_value(("output", "输出"))
    if output_target is not None:
        output_target = _clean_prompt_path(output_target)
    marked_output = _extract_output_path(prompt)
    if marked_output is not None:
        output_target = marked_output
    if output_target:
        for marker in (
            "文件夹下面",
            "目录下面",
            "文件夹下",
            "目录下",
            "文件夹中",
            "目录中",
            "文件夹内",
            "目录内",
            "下面",
        ):
            if output_target.endswith(marker):
                output_target = _clean_prompt_path(output_target[: -len(marker)])
    if task_type == "reconstruct":
        execution_order = (
            ("inspect", "reconstruct")
            if "inspect_before_reconstruct" in constraints
            else ("reconstruct",)
        )
    elif task_type == "batch_reconstruct":
        execution_order = ("batch_reconstruct",)
    elif task_type in {"inspect", "read_only", "analyze", "compare", "report", "status"}:
        execution_order = ("read_only",)
    else:
        execution_order = (task_type,)
    authorization = {
        "status": "declared",
        "workspace_id": request_hash(str(workspace.resolve()), "workspace"),
        "constraints": list(constraints),
        "constraint_fragments": list(fragments),
        "execution_order": list(execution_order),
        "allowed_read": ["workspace"],
        "allowed_write": list(
            ("job_ledger",)
            if task_type in {"cancel", "resume"}
            else ()
            if task_type in {"inspect", "read_only", "analyze", "compare", "report", "status"}
            else ("job_ledger", "run_output")
        ),
        "reference_policy": (
            "forbidden" if "no_reference_support" in constraints else "declared_only"
        ),
    }
    return IntentFrame(
        task_type=task_type,
        input_path=input_path,
        constraints=tuple(constraints),
        medium=medium,
        biomass=biomass,
        reference_policy="forbidden" if "no_reference_support" in constraints else "declared_only",
        budgets=budgets,
        allowed_read=("workspace",),
        allowed_write=("job_ledger",) if task_type in {"cancel", "resume"} else (
            ()
            if task_type in {"inspect", "read_only", "analyze", "compare", "report", "status"}
            else ("job_ledger", "run_output")
        ),
        ambiguities=tuple(ambiguities),
        constraint_fragments=tuple(fragments),
        execution_order=execution_order,
        output_target=output_target,
        authorization=authorization,
        inspection_path=inspection_path,
        reconstruction_input_path=reconstruction_input_path,
        job_id=job_id,
    )


def route_approval_id(decision: RouteDecision, model: str | None = None) -> str:
    """Return the stable approval token for a needs_approval decision."""
    if decision.status != "needs_approval":
        raise ContractError("an approval token is only defined for needs_approval routes")
    authorization = decision.authorization or {}
    request_id = str(authorization.get("request_hash", ""))
    workspace_id = str(authorization.get("workspace_id", ""))
    return request_hash(
        f"{request_id}:{workspace_id}:{decision.status}:{decision.reason}", model or ""
    )


def continue_approved_route(
    prompt: str,
    workspace: Path,
    approval_id: str,
    inspection_result: dict[str, object],
    *,
    model: str | None = None,
) -> RouteDecision:
    """Continue an inspect-before-reconstruct route after explicit approval.

    The inspection result is treated as evidence, not as authorization.  The
    inspected file's current hash must match the result before a reconstruction
    config artifact is materialized.
    """
    decision = route_request(prompt, workspace, model=model)
    if decision.status != "needs_approval":
        raise ContractError(f"route does not require approval: {decision.status}")
    if approval_id != route_approval_id(decision, model):
        raise ContractError("approval_id does not match the request contract")
    if not isinstance(inspection_result, dict):
        raise ContractError("inspection_result must be an object")
    status = str(inspection_result.get("status", "")).casefold()
    if status not in {"completed", "ok", "pass", "passed"}:
        raise ContractError("inspection evidence is not successful")
    authorization = dict(decision.authorization or {})
    inspection_path = authorization.get("inspection_path")
    reconstruction_path = authorization.get("reconstruction_input_path")
    if not isinstance(inspection_path, str) or not inspection_path:
        raise ContractError("approval plan has no inspection path")
    if not isinstance(reconstruction_path, str) or not reconstruction_path:
        raise ContractError("approval plan has no reconstruction input")
    inspected = _resolve(workspace, inspection_path)
    expected_hash = inspection_result.get("model_sha256")
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ContractError("inspection evidence lacks model_sha256")
    if not inspected.is_file() or metabolic_hash(inspected) != expected_hash:
        raise ContractError("inspection evidence is stale for the current model")
    intent = compile_intent_frame(prompt, workspace)
    config: dict[str, object] = {
        "input": reconstruction_path,
        "input_type": "auto",
        "engine": "native",
        "allow_ambiguous_ec_gpr": True,
        "annotation": (
            "ncbi-hmm"
            if _input_type(_resolve(workspace, reconstruction_path), "auto") == "faa"
            else "pgap"
        ),
    }
    if intent.medium is not None:
        config["medium"] = intent.medium
    if intent.biomass is not None:
        config["biomass_template"] = intent.biomass
    if intent.output_target is not None:
        config["output"] = intent.output_target
    config.update(intent.budgets)
    if intent.reference_policy == "forbidden":
        config["reference_support"] = False
    config = apply_reconstruction_defaults(config, workspace)
    contract = compile_contract(config, workspace)
    config_path = _materialize_route_config(workspace, contract, config)
    contract = compile_contract(config, workspace, config_path=config_path)
    authorization.update(
        {
            "status": "approved_after_inspection",
            "approval_id": approval_id,
            "inspection_result": inspection_result,
            "contract_id": contract.contract_id,
            "request_hash": request_hash(prompt, model or ""),
        }
    )
    return RouteDecision(
        "ready",
        "metabolic_start",
        "inspection completed and reconstruction approved",
        contract,
        config,
        authorization,
    )
