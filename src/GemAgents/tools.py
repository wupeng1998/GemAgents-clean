from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse

import requests

from GemAgents.errors import ToolError
from GemAgents.file_history import FileHistory
from GemAgents.hooks import HookBlocked, run_hooks
from GemAgents.mcp import McpRegistry
from GemAgents.metabolic import legacy as _metabolic_legacy
from GemAgents.metabolic.analysis import (
    analyze_request,
    compare_models,
    compare_scenarios,
    model_inspect,
    render_report,
    simulate_fba,
    simulate_fva,
    simulate_pfba,
)
from GemAgents.metabolic.analysis_tools import (
    analyze_shadow_prices,
    inspect_model_component,
    scan_essentiality,
    simulate_knockouts,
)
from GemAgents.metabolic.jobs.batch import metabolic_batch_start, metabolic_batch_status
from GemAgents.metabolic.jobs.contracts import (
    ContractError,
    ToolResult,
    compile_config_file,
    load_configuration,
    route_request,
    tool_spec_map,
    validate_tool_input,
    validate_tool_output,
)
from GemAgents.metabolic.jobs.runtime import metabolic_cancel, metabolic_status
from GemAgents.metabolic.legacy import metabolic_resume, metabolic_start
from GemAgents.metabolic.qc import Result as Result
from GemAgents.permissions import PermissionCallback, PermissionMode, decide_permission
from GemAgents.state import RuntimeState

_NETWORK_MAX_BYTES = 2 * 1024 * 1024


def _valid_reference_items(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


@dataclass
class LocalToolRunner:
    workspace: Path
    mode: PermissionMode = PermissionMode.DEFAULT
    ask_permission: PermissionCallback | None = None
    shell_timeout: int = 30
    runtime_state: RuntimeState | None = None
    file_history: FileHistory | None = None
    mcp_registry: McpRegistry | None = None
    hooks: dict[str, object] | None = None
    command_env: dict[str, str] | None = None
    additional_directories: list[Path] | None = None
    respect_gitignore: bool = True
    audit_events: list[dict[str, object]] | None = None
    request_id: str | None = None
    enforce_authorized_contract: bool = False
    authorized_contract_id: str | None = None
    authorized_authorization: dict[str, object] | None = None

    def run(self, tool_name: str, tool_input: dict[str, object]) -> str:
        try:
            validate_tool_input(tool_name, tool_input)
        except ContractError as error:
            self._record_audit(tool_name, tool_input, status="rejected", error=error)
            raise ToolError(str(error)) from error
        decision = decide_permission(
            tool_name,
            tool_input,
            mode=self.mode,
            ask=self.ask_permission,
        )
        if not decision.allowed:
            self._record_audit(
                tool_name,
                tool_input,
                status="denied",
                error=ToolError(decision.reason),
            )
            raise ToolError(f"{tool_name} denied: {decision.reason}")

        self._run_hook("PreToolUse", tool_name, tool_input)
        result: str | None = None
        try:
            result = self._run_allowed(tool_name, tool_input)
            if tool_name in tool_spec_map():
                result = self._ensure_tool_result(result)
            self._validate_registered_output(tool_name, result)
        except HookBlocked as error:
            self._record_audit(tool_name, tool_input, status="blocked", error=error)
            raise ToolError(str(error)) from error
        except Exception as error:
            self._record_audit(tool_name, tool_input, status="failed", error=error)
            raise
        try:
            self._run_hook("PostToolUse", tool_name, {"input": tool_input, "result": result})
        except HookBlocked as error:
            self._record_audit(tool_name, tool_input, status="blocked", error=error)
            raise ToolError(str(error)) from error
        self._record_audit(tool_name, tool_input, status="completed", result=result)
        return result

    def _record_audit(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        *,
        status: str,
        result: str | None = None,
        error: Exception | None = None,
    ) -> None:
        if self.audit_events is None:
            return
        payload = self._output_payload(result or "")
        contract = payload.get("contract") if isinstance(payload, dict) else None
        contract_id = payload.get("contract_id") if isinstance(payload, dict) else None
        if contract_id is None and isinstance(contract, dict):
            contract_id = contract.get("contract_id")
        event: dict[str, object] = {
            "tool": tool_name,
            "status": status,
            "request_id": self.request_id,
            "contract_id": contract_id or tool_input.get("contract_id"),
            "job_id": (
                payload.get("job_id") if isinstance(payload, dict) else tool_input.get("job_id")
            ),
            "evidence_refs": payload.get("evidence_refs", [])
            if isinstance(payload, dict)
            else [],
        }
        if error is not None:
            event["error_type"] = type(error).__name__
            event["error"] = str(error)[:500]
        self.audit_events.append(event)

    @staticmethod
    def _output_payload(result: str) -> dict[str, object]:
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _ensure_tool_result(cls, result: str) -> str:
        """Add the common structured envelope while preserving legacy fields.

        Domain callers historically consume top-level fields such as ``job_id``
        and ``model_sha256``.  Keep those fields for compatibility, but make
        every registered tool expose the same ``ToolResult`` payload for audit
        and downstream artifact handling.
        """
        payload = cls._output_payload(result)
        if not payload:
            return result
        if "artifacts" in payload and not isinstance(payload["artifacts"], list):
            raise ToolError("ToolResult artifacts must be an array")
        if "artifacts" in payload and not _valid_reference_items(payload["artifacts"]):
            raise ToolError("ToolResult artifacts must contain only strings")
        if "evidence_refs" in payload and not isinstance(payload["evidence_refs"], list):
            raise ToolError("ToolResult evidence_refs must be an array")
        if "evidence_refs" in payload and not _valid_reference_items(
            payload["evidence_refs"]
        ):
            raise ToolError("ToolResult evidence_refs must contain only strings")
        if "status" in payload and not isinstance(payload["status"], str):
            raise ToolError("ToolResult status must be a string")
        if "retryable" in payload and type(payload["retryable"]) is not bool:
            raise ToolError("ToolResult retryable must be a boolean")
        if "cost" in payload and payload["cost"] is not None and not isinstance(
            payload["cost"], dict
        ):
            raise ToolError("ToolResult cost must be an object or null")
        if "error" in payload and payload["error"] is not None and not isinstance(
            payload["error"], dict
        ):
            raise ToolError("ToolResult error must be an object or null")
        if all(
            key in payload
            for key in ("data", "artifacts", "evidence_refs", "error", "retryable", "cost")
        ):
            return result
        envelope = ToolResult(
            status=str(payload.get("status", "completed")),
            data=payload,
            artifacts=tuple(
                item for item in payload.get("artifacts", [])
            ),
            evidence_refs=tuple(
                item for item in payload.get("evidence_refs", [])
            ),
            error=payload.get("error") if isinstance(payload.get("error"), dict) else None,
            retryable=bool(payload.get("retryable", False)),
            cost=payload.get("cost") if isinstance(payload.get("cost"), dict) else None,
        ).as_dict()
        return json.dumps({**payload, **envelope}, ensure_ascii=False)

    @classmethod
    def _validate_registered_output(cls, tool_name: str, result: str) -> None:
        spec_payload = cls._output_payload(result)
        if not spec_payload:
            # Unregistered tools and human-readable tools keep their existing
            # text response. Registered domain tools must return JSON.
            if tool_name in tool_spec_map():
                raise ToolError(f"{tool_name} returned a non-object JSON result")
            return
        required_envelope = {
            "status", "data", "artifacts", "evidence_refs", "error", "retryable", "cost"
        }
        missing_envelope = required_envelope - set(spec_payload)
        if missing_envelope:
            raise ToolError(
                f"{tool_name} output missing ToolResult fields: "
                + ", ".join(sorted(missing_envelope))
            )
        if not isinstance(spec_payload["artifacts"], list):
            raise ToolError(f"{tool_name} output artifacts must be an array")
        if not _valid_reference_items(spec_payload["artifacts"]):
            raise ToolError(f"{tool_name} output artifacts must contain only strings")
        if not isinstance(spec_payload["evidence_refs"], list):
            raise ToolError(f"{tool_name} output evidence_refs must be an array")
        if not _valid_reference_items(spec_payload["evidence_refs"]):
            raise ToolError(f"{tool_name} output evidence_refs must contain only strings")
        if not isinstance(spec_payload["status"], str):
            raise ToolError(f"{tool_name} output status must be a string")
        if spec_payload["error"] is not None and not isinstance(spec_payload["error"], dict):
            raise ToolError(f"{tool_name} output error must be an object or null")
        if type(spec_payload["retryable"]) is not bool:
            raise ToolError(f"{tool_name} output retryable must be a boolean")
        if spec_payload["cost"] is not None and not isinstance(spec_payload["cost"], dict):
            raise ToolError(f"{tool_name} output cost must be an object or null")
        try:
            validate_tool_output(tool_name, spec_payload)
        except ContractError as error:
            raise ToolError(f"{tool_name} output violates its schema: {error}") from error

    def _validate_authorized_job_control(self, tool_name: str, job_id: str) -> None:
        """Keep resume/cancel calls bound to the deterministic route decision."""
        if not self.enforce_authorized_contract:
            return
        authorization = self.authorized_authorization
        if not isinstance(authorization, dict):
            raise ToolError("job control is missing an authorized job control request")
        expected_task = {
            "metabolic_cancel": "cancel",
            "metabolic_resume": "resume",
        }[tool_name]
        if authorization.get("status") != "job_control_request":
            raise ToolError("job control is missing an authorized job control request")
        if authorization.get("task_type") != expected_task:
            raise ToolError("authorized job control kind does not match the requested tool")
        if authorization.get("job_id") != job_id:
            raise ToolError("job control target differs from the authorized job")

    def _validate_authorized_status(self, job_id: str) -> None:
        """Keep a status follow-up bound to the routed job target."""
        if not self.enforce_authorized_contract:
            return
        authorization = self.authorized_authorization
        if not isinstance(authorization, dict):
            raise ToolError("job status is missing an authorized status request")
        if (
            authorization.get("status") != "job_status_request"
            or authorization.get("task_type") != "status"
        ):
            raise ToolError("job status is missing an authorized status request")
        if authorization.get("job_id") != job_id:
            raise ToolError("job status target differs from the authorized job")

    def _run_allowed(self, tool_name: str, tool_input: dict[str, object]) -> str:
        if tool_name == "read_file":
            return self._read_file(str(tool_input["path"]), int(tool_input.get("limit", 20_000)))
        if tool_name == "write_file":
            return self._write_file(str(tool_input["path"]), str(tool_input["content"]))
        if tool_name == "edit_file":
            return self._edit_file(
                str(tool_input["path"]),
                str(tool_input["old"]),
                str(tool_input["new"]),
            )
        if tool_name == "multi_edit":
            return self._multi_edit(str(tool_input["path"]), _list_of_dicts(tool_input["edits"]))
        if tool_name == "list_files":
            return self._list_files(
                str(tool_input.get("path", ".")),
                str(tool_input.get("pattern", "*")),
            )
        if tool_name == "grep":
            return self._grep(str(tool_input["pattern"]), str(tool_input.get("path", ".")))
        if tool_name == "run_shell":
            return self._run_shell(str(tool_input["command"]))
        if tool_name == "run_powershell":
            return self._run_powershell(str(tool_input["command"]))
        if tool_name == "metabolic_start":
            if self.enforce_authorized_contract and self.authorized_contract_id is None:
                raise ToolError("reconstruction is not authorized by the request contract")
            if self.enforce_authorized_contract and self.authorized_authorization is None:
                raise ToolError("reconstruction is missing the request authorization summary")
            config_path = self._resolve(str(tool_input["config_path"]))
            try:
                config = load_configuration(config_path)
                contract = compile_config_file(config_path, self.workspace)
            except (ContractError, ValueError, OSError) as error:
                raise ToolError(str(error)) from error
            if (
                self.enforce_authorized_contract
                and contract.contract_id != self.authorized_contract_id
            ):
                raise ToolError("reconstruction config differs from the authorized contract")
            authorization = self.authorized_authorization
            if self.enforce_authorized_contract and authorization is not None:
                if authorization.get("contract_id") != contract.contract_id:
                    raise ToolError(
                        "request authorization does not match the reconstruction contract"
                    )
                medium = authorization.get("medium")
                if medium is not None and config.get("medium", "minimal") != medium:
                    raise ToolError("reconstruction medium differs from the authorized request")
                biomass = authorization.get("biomass")
                if biomass is not None and config.get("biomass_template") != biomass:
                    raise ToolError("reconstruction biomass differs from the authorized request")
                budgets = authorization.get("budgets", {})
                if isinstance(budgets, dict):
                    for key, value in budgets.items():
                        if config.get(key) != value:
                            raise ToolError(f"reconstruction budget differs for {key}")
                if authorization.get("reference_policy") == "forbidden" and config.get(
                    "reference_support", True
                ) is not False:
                    raise ToolError("reference support is forbidden by the authorized request")
            contract_payload = contract.as_dict()
            if authorization is not None:
                contract_payload["authorization"] = authorization
            return metabolic_start(
                self,
                str(tool_input["config_path"]),
                contract=contract_payload,
            )
        if tool_name == "metabolic_batch_start":
            if self.enforce_authorized_contract:
                authorization = self.authorized_authorization
                if (
                    not isinstance(authorization, dict)
                    or authorization.get("status") != "batch_request_declared"
                ):
                    raise ToolError("batch reconstruction is not authorized by the request route")
                authorized_fields = {
                    "input_directory": "input_directory",
                    "output_directory": "output_target",
                    "mapping": "mapping",
                    "biomass_library": "biomass_library",
                    "reaction_library": "reaction_library",
                    "hmm_directory": "hmm_directory",
                }
                for field, authorized_field in authorized_fields.items():
                    if str(tool_input[field]) != str(authorization.get(authorized_field)):
                        raise ToolError(
                            f"batch {field} differs from the authorized request"
                        )
            return metabolic_batch_start(
                self,
                str(tool_input["input_directory"]),
                str(tool_input["output_directory"]),
                str(tool_input["mapping"]),
                str(tool_input["biomass_library"]),
                str(tool_input["reaction_library"]),
                str(tool_input["hmm_directory"]),
            )
        if tool_name == "metabolic_batch_status":
            return metabolic_batch_status(self, str(tool_input["job_id"]))
        if tool_name == "metabolic_resume":
            self._validate_authorized_job_control(tool_name, str(tool_input["job_id"]))
            return metabolic_resume(self, str(tool_input["job_id"]))
        if tool_name == "metabolic_status":
            job_id = str(tool_input["job_id"])
            self._validate_authorized_status(job_id)
            return metabolic_status(self, job_id)
        if tool_name == "metabolic_cancel":
            self._validate_authorized_job_control(tool_name, str(tool_input["job_id"]))
            return metabolic_cancel(
                self,
                str(tool_input["job_id"]),
                terminate=bool(tool_input.get("terminate", False)),
                grace_seconds=float(tool_input.get("grace_seconds", 5.0)),
            )
        if tool_name == "inspect_model_component":
            return json.dumps(
                inspect_model_component(
                    self._resolve(str(tool_input["model_path"])),
                    component=str(tool_input.get("component", "summary")),
                    identifier=(
                        str(tool_input["identifier"])
                        if tool_input.get("identifier") is not None
                        else None
                    ),
                    limit=int(tool_input.get("limit", 100)),
                ),
                ensure_ascii=False,
            )
        if tool_name == "simulate_knockouts":
            identifiers = tool_input["identifiers"]
            if not isinstance(identifiers, list):
                raise ToolError("simulate_knockouts.identifiers must be an array")
            medium = tool_input.get("medium")
            if medium is not None and not isinstance(medium, dict):
                raise ToolError("simulate_knockouts.medium must be an object")
            return json.dumps(
                simulate_knockouts(
                    self._resolve(str(tool_input["model_path"])),
                    kind=str(tool_input["kind"]),
                    identifiers=[str(item) for item in identifiers],
                    medium=medium,
                    objective_id=(
                        str(tool_input["objective_id"])
                        if tool_input.get("objective_id") is not None
                        else None
                    ),
                ),
                ensure_ascii=False,
            )
        if tool_name == "scan_essentiality":
            medium = tool_input.get("medium")
            if medium is not None and not isinstance(medium, dict):
                raise ToolError("scan_essentiality.medium must be an object")
            return json.dumps(
                scan_essentiality(
                    self._resolve(str(tool_input["model_path"])),
                    kind=str(tool_input.get("kind", "gene")),
                    threshold=float(tool_input.get("threshold", 0.1)),
                    max_items=int(tool_input.get("max_items", 1000)),
                    medium=medium,
                    objective_id=(
                        str(tool_input["objective_id"])
                        if tool_input.get("objective_id") is not None
                        else None
                    ),
                ),
                ensure_ascii=False,
            )
        if tool_name == "analyze_shadow_prices":
            medium = tool_input.get("medium")
            if medium is not None and not isinstance(medium, dict):
                raise ToolError("analyze_shadow_prices.medium must be an object")
            return json.dumps(
                analyze_shadow_prices(
                    self._resolve(str(tool_input["model_path"])),
                    medium=medium,
                    objective_id=(
                        str(tool_input["objective_id"])
                        if tool_input.get("objective_id") is not None
                        else None
                    ),
                    limit=int(tool_input.get("limit", 5000)),
                ),
                ensure_ascii=False,
            )
        if tool_name == "reconstruction_route":
            decision = route_request(str(tool_input["request"]), self.workspace)
            return json.dumps(decision.as_dict(), ensure_ascii=False)
        if tool_name == "model_inspect":
            return json.dumps(model_inspect(self._resolve(str(tool_input["model_path"]))))
        if tool_name == "simulate_fba":
            return json.dumps(
                simulate_fba(
                    self._resolve(str(tool_input["model_path"])),
                    medium=tool_input.get("medium"),
                    objective_id=tool_input.get("objective_id"),
                    maintenance=tool_input.get("maintenance"),
                    maintenance_reaction_id=tool_input.get("maintenance_reaction_id"),
                    solver_tolerance=tool_input.get("solver_tolerance"),
                )
            )
        if tool_name == "simulate_pfba":
            return json.dumps(
                simulate_pfba(
                    self._resolve(str(tool_input["model_path"])),
                    medium=tool_input.get("medium"),
                    objective_id=tool_input.get("objective_id"),
                    maintenance=tool_input.get("maintenance"),
                    maintenance_reaction_id=tool_input.get("maintenance_reaction_id"),
                    solver_tolerance=tool_input.get("solver_tolerance"),
                )
            )
        if tool_name == "simulate_fva":
            return json.dumps(
                simulate_fva(
                    self._resolve(str(tool_input["model_path"])),
                    fraction_of_optimum=float(tool_input.get("fraction_of_optimum", 1.0)),
                    medium=tool_input.get("medium"),
                    objective_id=tool_input.get("objective_id"),
                    maintenance=tool_input.get("maintenance"),
                    maintenance_reaction_id=tool_input.get("maintenance_reaction_id"),
                    solver_tolerance=tool_input.get("solver_tolerance"),
                )
            )
        if tool_name == "compare_scenarios":
            scenarios = tool_input["scenarios"]
            if not isinstance(scenarios, dict):
                raise ToolError("compare_scenarios.scenarios must be an object")
            return json.dumps(
                compare_scenarios(
                    self._resolve(str(tool_input["model_path"])),
                    scenarios,
                    objective_id=tool_input.get("objective_id"),
                    maintenance=tool_input.get("maintenance"),
                    maintenance_reaction_id=tool_input.get("maintenance_reaction_id"),
                    solver_tolerance=tool_input.get("solver_tolerance"),
                )
            )
        if tool_name == "compare_models":
            models = tool_input["models"]
            if not isinstance(models, dict):
                raise ToolError("compare_models.models must be an object")
            return json.dumps(
                compare_models(
                    {str(name): self._resolve(str(path)) for name, path in models.items()}
                )
            )
        if tool_name == "render_report":
            result = tool_input["result"]
            if not isinstance(result, dict):
                raise ToolError("render_report.result must be an object")
            return json.dumps(render_report(result))
        if tool_name == "analyze_request":
            scenarios = tool_input.get("scenarios")
            if scenarios is not None and not isinstance(scenarios, dict):
                raise ToolError("analyze_request.scenarios must be an object")
            medium = tool_input.get("medium")
            if medium is not None and not isinstance(medium, dict):
                raise ToolError("analyze_request.medium must be an object")
            return json.dumps(
                analyze_request(
                    str(tool_input["request"]),
                    self._resolve(str(tool_input["model_path"])),
                    scenarios=scenarios,
                    network_model_path=(
                        self._resolve(str(tool_input["network_model_path"]))
                        if tool_input.get("network_model_path") is not None
                        else None
                    ),
                    substrate_id=(
                        str(tool_input["substrate_id"])
                        if tool_input.get("substrate_id") is not None
                        else None
                    ),
                    product_id=(
                        str(tool_input["product_id"])
                        if tool_input.get("product_id") is not None
                        else None
                    ),
                    number_of_optimizations=int(
                        tool_input.get("number_of_optimizations", 10)
                    ),
                    min_fraction=float(tool_input.get("min_fraction", 0.1)),
                    medium=medium,
                    biomass_id=(
                        str(tool_input["biomass_id"])
                        if tool_input.get("biomass_id") is not None
                        else None
                    ),
                    objective_id=(
                        str(tool_input["objective_id"])
                        if tool_input.get("objective_id") is not None
                        else None
                    ),
                    steps=int(tool_input.get("steps", 30)),
                    use_fva=bool(tool_input.get("use_fva", False)),
                    constrain_biomass=bool(tool_input.get("constrain_biomass", False)),
                    max_flux_cutoff=float(tool_input.get("max_flux_cutoff", 0.95)),
                    fraction_of_optimum=float(
                        tool_input.get("fraction_of_optimum", 1.0)
                    ),
                    maintenance=tool_input.get("maintenance"),
                    maintenance_reaction_id=tool_input.get("maintenance_reaction_id"),
                    solver_tolerance=tool_input.get("solver_tolerance"),
                    community_models=(
                        {
                            str(name): self._resolve(str(path))
                            for name, path in tool_input.get("community_models", {}).items()
                        }
                        if tool_input.get("community_models") is not None
                        else None
                    ),
                    strain_design_type=(
                        str(tool_input["strain_design_type"])
                        if tool_input.get("strain_design_type") is not None
                        else None
                    ),
                    strain_design_config=(
                        dict(tool_input["strain_design_config"])
                        if tool_input.get("strain_design_config") is not None
                        else None
                    ),
                )
            )
        if tool_name == "web_fetch":
            return self._web_fetch(str(tool_input["url"]))
        if tool_name == "web_search":
            return self._web_search(str(tool_input["query"]))
        if tool_name == "weather":
            return self._weather(str(tool_input["location"]))
        if tool_name == "todo_write":
            return self._state().set_todos(_list_of_dicts(tool_input.get("items", [])))
        if tool_name == "task_create":
            return self._state().create_task(str(tool_input["id"]), str(tool_input["description"]))
        if tool_name == "task_update":
            return self._state().update_task(
                str(tool_input["id"]),
                str(tool_input["status"]),
                None if tool_input.get("result") is None else str(tool_input["result"]),
            )
        if tool_name == "task_list":
            return self._state().list_tasks()
        if tool_name == "task_get":
            task = self._state().get_task(str(tool_input["id"]))
            return f"{task.id}\t{task.status}\t{task.description}\t{task.result or ''}"
        if tool_name == "memory_write":
            return self._state().write_memory(str(tool_input["content"]))
        if tool_name == "list_mcp_resources":
            return self._mcp().list_resources()
        if tool_name == "read_mcp_resource":
            return self._mcp().read_resource(str(tool_input["uri"]))
        if tool_name == "restore_file":
            if self.file_history is None:
                raise ToolError("file history is not enabled")
            return self.file_history.restore(str(tool_input["snapshot_id"]))
        if tool_name in {"agent", "team_create", "team_delete", "send_message"}:
            return self._team_tool(tool_name, tool_input)

        raise ToolError(f"unknown tool: {tool_name}")

    def _read_file(self, path: str, limit: int = 20_000) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        return target.read_text(encoding="utf-8")[:limit]

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        self._snapshot(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {self._display_path(target)} ({len(content)} bytes)"

    def _edit_file(self, path: str, old: str, new: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")

        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old)
        if occurrences != 1:
            raise ToolError(f"expected exactly one match, found {occurrences}")

        self._snapshot(target)
        target.write_text(content.replace(old, new), encoding="utf-8")
        return f"edited {self._display_path(target)}"

    def _multi_edit(self, path: str, edits: list[dict[str, object]]) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        content = target.read_text(encoding="utf-8")
        for edit in edits:
            old = str(edit["old"])
            new = str(edit["new"])
            occurrences = content.count(old)
            if occurrences != 1:
                raise ToolError(f"expected exactly one match for {old!r}, found {occurrences}")
            content = content.replace(old, new)
        self._snapshot(target)
        target.write_text(content, encoding="utf-8")
        return f"applied {len(edits)} edit(s) to {self._display_path(target)}"

    def _list_files(self, path: str = ".", pattern: str = "*") -> str:
        root = self._resolve(path)
        if not root.exists():
            raise ToolError(f"path does not exist: {path}")

        files: Iterable[Path]
        if root.is_file():
            files = [root]
        else:
            files = sorted(
                p for p in root.rglob(pattern) if p.is_file() and not self._is_ignored(p)
            )

        rel_paths = [self._display_path(p) for p in files]
        return "\n".join(rel_paths[:500])

    def _grep(self, pattern: str, path: str = ".") -> str:
        root = self._resolve(path)
        if not root.exists():
            raise ToolError(f"path does not exist: {path}")

        rg = shutil.which("rg")
        if rg:
            args = [rg, "--line-number", "--no-heading"]
            if not self.respect_gitignore:
                args.append("--no-ignore")
            args.extend([pattern, str(root)])
            completed = subprocess.run(
                args,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.shell_timeout,
                check=False,
            )
            output = completed.stdout or completed.stderr
            return self._truncate(output)

        matches: list[str] = []
        files = [root] if root.is_file() else root.rglob("*")
        for file_path in files:
            if not file_path.is_file() or self._is_ignored(file_path):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, start=1):
                if pattern in line:
                    matches.append(f"{self._display_path(file_path)}:{index}:{line}")
        return self._truncate("\n".join(matches))

    def _run_shell(self, command: str) -> str:
        _reject_dangerous_shell(command)
        completed = subprocess.run(
            command,
            cwd=self.workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=self.shell_timeout,
            env=self._command_env(),
            check=False,
        )
        parts = [f"exit_code={completed.returncode}"]
        if completed.stdout:
            parts.append("stdout:\n" + completed.stdout)
        if completed.stderr:
            parts.append("stderr:\n" + completed.stderr)
        return self._truncate("\n".join(parts))

    def _run_powershell(self, command: str) -> str:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise ToolError("PowerShell executable was not found")
        completed = subprocess.run(
            [executable, "-NoProfile", "-Command", command],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=self.shell_timeout,
            env=self._command_env(),
            check=False,
        )
        return self._truncate(
            f"exit_code={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )

    def _web_fetch(self, url: str) -> str:
        try:
            response = self._safe_web_get(url)
            return self._bounded_response_text(response)
        except (requests.RequestException, ToolError) as error:
            raise ToolError(f"web_fetch failed: {error}") from error

    def _web_search(self, query: str) -> str:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        try:
            response = self._safe_web_get(url)
            html = self._bounded_response_text(response)
        except (requests.RequestException, ToolError) as error:
            raise ToolError(f"web_search failed: {error}") from error
        parsed = _parse_duckduckgo_results(html, query)
        return parsed or self._truncate(html)

    def _weather(self, location: str) -> str:
        url = f"https://wttr.in/{quote(location)}?format=j1"
        try:
            response = self._safe_web_get(url)
            payload = self._bounded_response_json(response)
            return _format_weather(location, payload, url)
        except (requests.RequestException, json.JSONDecodeError, ToolError) as primary_error:
            fallback_url = _tianqi_url(location)
            if fallback_url is None:
                raise ToolError(f"weather failed: {primary_error}") from primary_error
            try:
                response = self._safe_web_get(fallback_url)
                return _parse_tianqi_weather(
                    location, self._bounded_response_text(response), fallback_url
                )
            except (requests.RequestException, ToolError) as fallback_error:
                raise ToolError(
                    f"weather failed: {primary_error}; fallback failed: {fallback_error}"
                ) from fallback_error

    @staticmethod
    def _safe_web_get(url: str) -> requests.Response:
        _validate_public_http_url(url)
        try:
            response = requests.get(
                url,
                timeout=(5, 20),
                headers={"User-Agent": "gemagents/0.1"},
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException:
            raise
        status_code = getattr(response, "status_code", 200)
        if 300 <= status_code < 400:
            raise ToolError("redirects are not allowed for network tools")
        headers = getattr(response, "headers", {}) or {}
        raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
        try:
            if raw_length is not None and int(raw_length) > _NETWORK_MAX_BYTES:
                raise ToolError("network response exceeds the configured size limit")
        except (TypeError, ValueError) as error:
            raise ToolError("network response has an invalid Content-Length") from error
        response.raise_for_status()
        return response

    @staticmethod
    def _bounded_response_text(response: requests.Response) -> str:
        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            chunks: list[bytes] = []
            total = 0
            for chunk in iterator(chunk_size=64 * 1024):
                if not chunk:
                    continue
                piece = chunk if isinstance(chunk, bytes) else str(chunk).encode()
                total += len(piece)
                if total > _NETWORK_MAX_BYTES:
                    raise ToolError("network response exceeds the configured size limit")
                chunks.append(piece)
            return b"".join(chunks).decode("utf-8", errors="replace")
        text = getattr(response, "text", "")
        encoded = str(text).encode("utf-8")
        if len(encoded) > _NETWORK_MAX_BYTES:
            raise ToolError("network response exceeds the configured size limit")
        return str(text)

    @classmethod
    def _bounded_response_json(cls, response: requests.Response) -> object:
        if callable(getattr(response, "iter_content", None)) or hasattr(response, "text"):
            return json.loads(cls._bounded_response_text(response))
        return response.json()

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        allowed_roots = [self.workspace, *(self.additional_directories or [])]
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ToolError(f"path escapes workspace: {path}")
        return resolved

    def _command_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.command_env or {})
        return env

    def _is_ignored(self, path: Path) -> bool:
        if not self.respect_gitignore:
            return False
        relative = path.relative_to(self.workspace) if self.workspace in path.parents else path
        for pattern in _gitignore_patterns(self.workspace):
            if fnmatch(relative.as_posix(), pattern) or fnmatch(path.name, pattern):
                return True
        return False

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def _state(self) -> RuntimeState:
        if self.runtime_state is None:
            self.runtime_state = RuntimeState.for_workspace(self.workspace)
        return self.runtime_state

    def _mcp(self) -> McpRegistry:
        if self.mcp_registry is None:
            self.mcp_registry = McpRegistry(self.workspace)
        return self.mcp_registry

    def _snapshot(self, target: Path) -> None:
        if self.file_history is not None:
            self.file_history.snapshot(target)

    def _run_hook(self, event: str, tool_name: str, payload: dict[str, object]) -> None:
        if self.hooks:
            run_hooks(
                self.hooks,
                event,
                {"tool_name": tool_name, **payload},
                workspace=self.workspace,
                matcher=tool_name,
            )

    def _team_tool(self, tool_name: str, tool_input: dict[str, object]) -> str:
        state = self._state()
        if tool_name == "agent":
            task_id = str(tool_input.get("name", f"agent-{len(state.tasks) + 1}"))
            return state.create_task(task_id, str(tool_input.get("prompt", "")))
        if tool_name == "team_create":
            return state.create_task(str(tool_input["team_name"]), "team session")
        if tool_name == "team_delete":
            state.clear_tasks()
            return "deleted active team"
        if tool_name == "send_message":
            state.write_memory(f"{tool_input.get('to')}: {tool_input.get('message')}")
            return "message sent"
        raise ToolError(f"unknown team tool: {tool_name}")

    @staticmethod
    def _truncate(value: str, limit: int = 20_000) -> str:
        if len(value) <= limit:
            return value
        return value[:limit] + "\n[truncated]"


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ToolError("expected a list of objects")
    return value


def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ToolError("network URL must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ToolError("network URL must contain a public hostname without credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise ToolError("network URL has an invalid port") from error
    if port is not None and port not in {80, 443}:
        raise ToolError("network URL port is not allowed")
    host = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
            ]
        except (OSError, ValueError) as error:
            raise ToolError("network hostname could not be resolved safely") from error
    if any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    ):
        raise ToolError("network URL resolves to a non-public address")


def _reject_dangerous_shell(command: str) -> None:
    dangerous = ["rm -rf /", "mkfs", ":(){", "dd if=", "> /dev/sd", "shutdown", "reboot"]
    if any(pattern in command for pattern in dangerous):
        raise ToolError(f"dangerous shell command rejected: {command}")


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": _decode_duckduckgo_url(attributes.get("href", ""))}
            self._capture = "title"
        elif self._current is not None and "result__snippet" in classes:
            self._capture = "snippet"

    def handle_data(self, data: str) -> None:
        if self._current is None or self._capture is None:
            return
        existing = self._current.get(self._capture, "")
        self._current[self._capture] = f"{existing} {data.strip()}".strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture == "title":
            self._capture = None
        elif self._capture == "snippet" and tag in {"a", "div"}:
            self._capture = None
            if self._current is not None:
                self.results.append(self._current)
                self._current = None


def _parse_duckduckgo_results(html: str, query: str, *, limit: int = 5) -> str:
    parser = _DuckDuckGoParser()
    parser.feed(html)
    results = [item for item in parser.results if item.get("title")]
    if not results:
        return ""

    lines = [f"Search results for: {query}"]
    for index, item in enumerate(results[:limit], start=1):
        lines.append(f"{index}. {unescape(item.get('title', ''))}")
        if item.get("url"):
            lines.append(f"   URL: {item['url']}")
        if item.get("snippet"):
            lines.append(f"   Snippet: {unescape(item['snippet'])}")
    return "\n".join(lines)


def _format_weather(location: str, payload: object, source_url: str) -> str:
    if not isinstance(payload, dict):
        raise ToolError("weather failed: response was not a JSON object")

    current_items = payload.get("current_condition")
    if (
        not isinstance(current_items, list)
        or not current_items
        or not isinstance(current_items[0], dict)
    ):
        raise ToolError("weather failed: missing current weather data")
    current = current_items[0]

    forecast_items = payload.get("weather")
    forecast = (
        forecast_items[0]
        if (
            isinstance(forecast_items, list)
            and forecast_items
            and isinstance(forecast_items[0], dict)
        )
        else {}
    )
    description = _first_weather_description(current)

    lines = [
        f"Weather for {location}",
        f"Source: {source_url}",
        f"Observed: {current.get('observation_time', 'unknown')}",
        f"Condition: {description}",
        f"Temperature: {current.get('temp_C', '?')}°C",
        f"Feels like: {current.get('FeelsLikeC', '?')}°C",
        f"Humidity: {current.get('humidity', '?')}%",
        f"Precipitation: {current.get('precipMM', '?')} mm",
        (
            f"Wind: {current.get('windspeedKmph', '?')} km/h {current.get('winddir16Point', '')}"
        ).strip(),
    ]
    if forecast:
        lines.append(
            f"Today: {forecast.get('mintempC', '?')}°C to {forecast.get('maxtempC', '?')}°C"
        )
    return "\n".join(lines)


def _tianqi_url(location: str) -> str | None:
    slugs = {
        "武汉": "wuhan",
        "wuhan": "wuhan",
    }
    slug = slugs.get(location.strip().lower()) or slugs.get(location.strip())
    if slug is None:
        return None
    return f"https://www.tianqi.com/{slug}/today/"


def _parse_tianqi_weather(location: str, html: str, source_url: str) -> str:
    current = re.search(
        r'<p class="now">\s*<b>(?P<temp>[^<]+)</b>\s*<i>(?P<unit>[^<]+)</i>',
        html,
    )
    condition = re.search(
        r"<span>\s*<b>(?P<condition>[^<]+)</b>\s*(?P<range>[^<]+℃)\s*</span>",
        html,
    )
    details_match = re.search(r'<dd class="shidu">(?P<details>.*?)</dd>', html, re.S)
    if current is None and condition is None and details_match is None:
        raise ToolError("weather failed: missing tianqi weather data")

    lines = [f"Weather for {location}", f"Source: {source_url}"]
    if condition is not None:
        lines.append(f"Condition: {unescape(condition.group('condition')).strip()}")
    if current is not None:
        lines.append(
            f"Temperature: {current.group('temp').strip()}{unescape(current.group('unit')).strip()}"
        )
    if condition is not None:
        lines.append(f"Today: {unescape(condition.group('range')).strip()}")
    if details_match is not None:
        details = re.findall(r"<b>(.*?)</b>", details_match.group("details"), re.S)
        for detail in details:
            text = _strip_html(detail)
            if text:
                lines.append(text)
    return "\n".join(lines)


def _strip_html(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _first_weather_description(current: dict[object, object]) -> str:
    descriptions = current.get("weatherDesc")
    if isinstance(descriptions, list) and descriptions and isinstance(descriptions[0], dict):
        value = descriptions[0].get("value")
        if value:
            return str(value).strip()
    return "unknown"


def _decode_duckduckgo_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg")
    if target:
        return unquote(target[0])
    return href


def _gitignore_patterns(workspace: Path) -> list[str]:
    path = workspace / ".gitignore"
    if not path.is_file():
        return []
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        patterns.append(stripped.rstrip("/"))
        if "/" not in stripped:
            patterns.append(f"**/{stripped.rstrip('/')}")
    return patterns


def __getattr__(name: str):
    """Preserve historical metabolic imports while implementations move by layer."""
    try:
        return getattr(_metabolic_legacy, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_metabolic_legacy)))


_NATIVE_TOOL_SYMBOLS = frozenset(globals())


class _CompatibilityModule(ModuleType):
    """Forward patched historical symbols to their migrated implementation."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _NATIVE_TOOL_SYMBOLS and hasattr(_metabolic_legacy, name):
            setattr(_metabolic_legacy, name, value)


sys.modules[__name__].__class__ = _CompatibilityModule
