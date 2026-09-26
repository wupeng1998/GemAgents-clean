from __future__ import annotations

import hashlib
import json
import operator
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from GemAgents.checkpoint import EventStore, request_hash
from GemAgents.config import AgentConfig
from GemAgents.context_metrics import compaction_metrics
from GemAgents.file_history import FileHistory
from GemAgents.layout import RepoLayout
from GemAgents.logging_policy import append_event
from GemAgents.mcp import McpRegistry
from GemAgents.metabolic.jobs.contracts import (
    ContractError,
    _materialize_route_config,
    continue_approved_route,
    route_approval_id,
    route_request,
    tool_spec_map,
)
from GemAgents.multimodal import image_content_block
from GemAgents.ollama import ensure_ollama_running
from GemAgents.permissions import MUTATING_TOOLS, PermissionCallback, PermissionMode
from GemAgents.state import RuntimeState
from GemAgents.tools import LocalToolRunner, ToolError

_TRACE_MAX_CHARS = 240


def _trace_text(value: object, *, limit: int = _TRACE_MAX_CHARS) -> str:
    """Return a bounded, non-chain-of-thought preview for an execution trace."""
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def _trace_input(value: object) -> object:
    """Keep tool arguments useful while avoiding large content or secret values."""
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                result["..."] = f"{len(value) - index} more fields"
                break
            key_text = str(key)
            lowered = key_text.lower()
            if any(
                secret in lowered for secret in ("token", "password", "secret", "api_key")
            ):
                result[key_text] = "[REDACTED]"
            elif isinstance(item, (dict, list, tuple)):
                result[key_text] = _trace_input(item)
            elif key_text in {"content", "command", "prompt", "request", "message"}:
                result[key_text] = _trace_text(item)
            else:
                result[key_text] = _trace_text(item) if isinstance(item, str) else item
        return result
    if isinstance(value, (list, tuple)):
        return [_trace_input(item) for item in value[:20]]
    return _trace_text(value) if isinstance(value, str) else value


def _trace_observation(value: object) -> object:
    """Summarize a tool result for humans without dumping its full payload."""
    if isinstance(value, (dict, list)):
        parsed: object = value
    else:
        try:
            parsed = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return _trace_text(value)
    if isinstance(parsed, dict):
        summary: dict[str, object] = {}
        for key in (
            "status",
            "phase",
            "job_id",
            "model_status",
            "execution_status",
            "completed_builds",
            "total_builds",
            "counts",
            "objective",
            "growth",
            "reactions",
            "metabolites",
            "genes",
            "error",
            "reason",
            "failure_reason",
            "model",
            "quality_status",
            "memote",
            "objective_value",
            "solver_status",
            "observed_worker_state",
        ):
            if key in parsed:
                summary[key] = _trace_input(parsed[key])
        for key in ("data", "result", "summary"):
            if isinstance(parsed.get(key), dict):
                summary[key] = _trace_observation(parsed[key])
        if summary:
            return summary
        return {"keys": list(parsed)[:20]}
    if isinstance(parsed, list):
        return {"type": "list", "items": len(parsed)}
    return _trace_text(parsed)


def _trace_progress(payload: dict[str, object]) -> dict[str, object]:
    """Condense worker progress into a factual observation for the trace."""
    result = payload.get("result")
    observation: dict[str, object] = {
        key: _trace_input(payload[key])
        for key in (
            "job_id", "status", "phase", "observed_worker_state",
            "completed_builds", "total_builds",
        )
        if key in payload
    }
    if result is not None:
        observation["result"] = _trace_observation(result)
    counts = payload.get("counts")
    if counts is not None:
        observation["counts"] = _trace_input(counts)
    if payload.get("monitoring_interrupted"):
        observation["monitoring_interrupted"] = True
    return observation


class ChatModel(Protocol):
    def invoke(self, messages: list[BaseMessage]) -> AIMessage: ...


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    steps: int
    tool_calls: int
    started_at: float
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    usage_available: bool
    token_usage_complete: bool
    cost_usage_complete: bool
    resume_node: str | None


@dataclass
class AgentResult:
    text: str
    messages: list[BaseMessage]
    steps: int
    stop_reason: str = "completed"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    token_usage_complete: bool = False
    cost_usage_complete: bool = False


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _extract_usage(response: AIMessage) -> dict[str, object]:
    """Read provider usage without inferring tokens or prices from message text."""
    usage: dict[str, object] = {}
    raw_usage = getattr(response, "usage_metadata", None)
    if isinstance(raw_usage, dict):
        usage.update(raw_usage)
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        nested = metadata.get("token_usage") or metadata.get("usage")
        if isinstance(nested, dict):
            for key, value in nested.items():
                usage.setdefault(key, value)
        for key in ("cost", "cost_usd", "estimated_cost"):
            if key in metadata:
                usage.setdefault(key, metadata[key])
    input_tokens = _as_nonnegative_int(
        usage.get("input_tokens", usage.get("prompt_tokens"))
    )
    output_tokens = _as_nonnegative_int(
        usage.get("output_tokens", usage.get("completion_tokens"))
    )
    total_tokens = _as_nonnegative_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cost_usd = _as_nonnegative_float(
        usage.get("cost_usd", usage.get("estimated_cost", usage.get("cost")))
    )
    return {
        "available": any(
            value is not None for value in (input_tokens, output_tokens, total_tokens, cost_usd)
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }


def _process_memory_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def _batch_status_marker(payload: dict[str, object]) -> tuple[object, ...]:
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    builds = summary.get("builds")
    latest_build = builds[-1] if isinstance(builds, list) and builds else {}
    latest_build = latest_build if isinstance(latest_build, dict) else {}
    counts = summary.get("counts")
    return (
        payload.get("status"),
        summary.get("completed_builds"),
        summary.get("total_builds"),
        json.dumps(counts, sort_keys=True, ensure_ascii=False),
        latest_build.get("id"),
        latest_build.get("status"),
    )


def _batch_log_delta(runner: LocalToolRunner, payload: dict[str, object], offset: int):
    log_path = payload.get("log")
    if not isinstance(log_path, str) or not log_path:
        return offset, []
    try:
        path = runner._resolve(log_path)
        with path.open("rb") as log:
            log.seek(offset)
            chunk = log.read(64 * 1024)
            offset = log.tell()
    except OSError:
        return offset, []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return offset, [line for line in lines if line]


def _submit_batch_and_wait(runner: LocalToolRunner, args: dict[str, str]) -> str:
    """Keep the interactive request attached to its batch until the worker exits."""
    submitted_text = runner.run("metabolic_batch_start", args)
    try:
        submitted = json.loads(submitted_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise ToolError("batch start returned an invalid job record") from error
    if not isinstance(submitted, dict) or not isinstance(submitted.get("job_id"), str):
        raise ToolError("batch start returned no job ID")

    job_id = submitted["job_id"]
    reporter = getattr(runner, "batch_progress_callback", None)
    offset = 0
    previous_marker = None
    last_heartbeat = time.monotonic()
    last_status: dict[str, object] = submitted
    while True:
        try:
            if callable(reporter) and previous_marker is None:
                reporter({"job_id": job_id, "status": submitted.get("status", "submitted")})
            status_text = runner.run("metabolic_batch_status", {"job_id": job_id})
            try:
                status = json.loads(status_text)
            except (TypeError, json.JSONDecodeError) as error:
                raise ToolError("batch status returned invalid JSON") from error
            if not isinstance(status, dict) or not isinstance(status.get("status"), str):
                raise ToolError("batch status returned no status field")
            last_status = status

            offset, log_lines = _batch_log_delta(runner, status, offset)
            marker = _batch_status_marker(status)
            now = time.monotonic()
            if callable(reporter) and (
                marker != previous_marker or log_lines or now - last_heartbeat >= 15
            ):
                summary = status.get("summary")
                summary = summary if isinstance(summary, dict) else {}
                reporter(
                    {
                        "job_id": job_id,
                        "status": status["status"],
                        "completed_builds": summary.get("completed_builds"),
                        "total_builds": summary.get("total_builds"),
                        "counts": summary.get("counts", {}),
                        "log_lines": log_lines,
                    }
                )
                previous_marker = marker
                last_heartbeat = now

            if status["status"] in {"completed", "failed", "cancelled"}:
                return status_text
            time.sleep(2)
        except KeyboardInterrupt:
            interrupted = dict(submitted)
            interrupted.update(
                {
                    "status": "submitted",
                    "monitoring_interrupted": True,
                    "last_observed_status": last_status.get("status", "submitted"),
                }
            )
            if callable(reporter):
                reporter(
                    {
                        "job_id": job_id,
                        "status": "submitted",
                        "monitoring_interrupted": True,
                        "last_observed_status": interrupted["last_observed_status"],
                        "log_lines": [],
                    }
                )
            return json.dumps(interrupted, ensure_ascii=False)


_ACTIVE_RECONSTRUCTION_STATES = {
    "reserved",
    "started",
    "running",
    "cancel_requested",
    "cancelling",
}


def _job_status_marker(payload: dict[str, object]) -> tuple[object, ...]:
    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    return (
        payload.get("status"),
        payload.get("phase"),
        payload.get("observed_worker_state"),
        result.get("status"),
        result.get("model_status"),
    )


def _run_status_for_monitor(
    runner: LocalToolRunner,
    job_id: str,
) -> str:
    """Read a just-started job under an internal, job-bound status grant."""
    original = getattr(runner, "authorized_authorization", None)
    if getattr(runner, "enforce_authorized_contract", False):
        authorization = dict(original) if isinstance(original, dict) else {}
        authorization.update(
            {"status": "job_status_request", "task_type": "status", "job_id": job_id}
        )
        runner.authorized_authorization = authorization
    try:
        return runner.run("metabolic_status", {"job_id": job_id})
    finally:
        if getattr(runner, "enforce_authorized_contract", False):
            runner.authorized_authorization = original


def _submit_reconstruction_and_wait(runner: LocalToolRunner, args: dict[str, str]) -> str:
    """Keep a single reconstruction request attached until its worker reaches a terminal state."""
    submitted_text = runner.run("metabolic_start", args)
    try:
        submitted = json.loads(submitted_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise ToolError("reconstruction start returned invalid JSON") from error
    if not isinstance(submitted, dict) or not isinstance(submitted.get("job_id"), str):
        raise ToolError("reconstruction start returned no job ID")

    job_id = submitted["job_id"]
    reporter = getattr(runner, "job_progress_callback", None)
    offset = 0
    previous_marker = None
    last_heartbeat = time.monotonic()
    last_status: dict[str, object] = submitted
    if callable(reporter):
        reporter(
            {
                "job_id": job_id,
                "status": submitted.get("status", "submitted"),
                "phase": submitted.get("phase"),
                "observed_worker_state": "submitted",
                "log_lines": [],
            }
        )
    while True:
        try:
            status_text = _run_status_for_monitor(runner, job_id)
            try:
                status = json.loads(status_text)
            except (TypeError, json.JSONDecodeError) as error:
                raise ToolError("reconstruction status returned invalid JSON") from error
            if not isinstance(status, dict) or not isinstance(status.get("status"), str):
                raise ToolError("reconstruction status returned no status field")
            last_status = status
            offset, log_lines = _batch_log_delta(runner, status, offset)
            marker = _job_status_marker(status)
            now = time.monotonic()
            if callable(reporter) and (
                marker != previous_marker or log_lines or now - last_heartbeat >= 15
            ):
                reporter(
                    {
                        "job_id": job_id,
                        "status": status["status"],
                        "phase": status.get("phase"),
                        "observed_worker_state": status.get("observed_worker_state"),
                        "result": status.get("result"),
                        "log_lines": log_lines,
                    }
                )
                previous_marker = marker
                last_heartbeat = now
            if status["status"] not in _ACTIVE_RECONSTRUCTION_STATES:
                return status_text
            time.sleep(2)
        except KeyboardInterrupt:
            interrupted = dict(submitted)
            interrupted.update(
                {
                    "status": "submitted",
                    "monitoring_interrupted": True,
                    "last_observed_status": last_status.get("status", "submitted"),
                }
            )
            if callable(reporter):
                reporter(
                    {
                        "job_id": job_id,
                        "status": "submitted",
                        "monitoring_interrupted": True,
                        "last_observed_status": interrupted["last_observed_status"],
                        "log_lines": [],
                    }
                )
            return json.dumps(interrupted, ensure_ascii=False)


SYSTEM_PROMPT = """You are GemAgents, an LLM orchestrator for a terminal workspace.
Understand the user's Chinese or English intent, choose the smallest applicable
tool plan, call tools to obtain evidence, and then explain the observed result.
Before calling tools, give a brief user-facing action summary: what you will
check or execute, and which observed result makes that action relevant. Keep
this to one or two sentences, not a private reasoning transcript. After tools
return, use their observations to select the next action or report the result.
Do not pretend to have run a tool and do not invent file contents, command
results, paths, job IDs, or model metrics. Preserve user-provided paths exactly;
the deterministic router validates assets and permissions before side effects.
An existing .xml or .sbml path is a model-analysis input. If the user asks for
its genes, reactions, metabolites, compartments, growth, model summary, or
MEMOTE score (including Chinese requests such as “输出模型的基因、反应、代谢物、
生长和 MEMOTE 数据”), treat the request as read-only model inspection even if
a parent directory contains words such as “build”, “builds”, “reconstruct”, or
“构建”. Never pass an existing SBML/XML model path to metabolic_start and never
try to parse it as FASTA. For this summary task, call model_inspect and
inspect_model_component for the requested component lists, use analyze_request
with an FBA/growth request for model-condition growth, and inspect adjacent
manifest/quality/MEMOTE artifacts with list_files/read_file when they exist.
Report item limits or missing artifacts explicitly; a missing or disabled MEMOTE
artifact means that no MEMOTE score is available, not a zero score. Distinguish
model-condition predictions from experimental growth measurements.
For a validated reconstruction or batch request, follow the injected contract
instruction and call the named metabolic tool instead of composing a different
configuration. For asynchronous metabolic jobs, the reconstruction and directory
batch start tools monitor their workers to a terminal state before returning;
the model should not create a polling loop or poll its status itself. Distinguish submitted,
running, completed, failed, and cancelled states in the final report. When a
user asks for a follow-up, query the persisted job record and report its current
evidence; do not ask whether to continue monitoring. Do not describe a monitored
job as merely "submitted" or tell the user to query its job ID; report the
terminal result, or explicitly state that foreground monitoring was interrupted.
A worker's deterministic
annotation and reconstruction are evidence-producing domain operations; your
role is to schedule and operate those tools, not to fabricate annotation,
GPR evidence, or quality certificates. For model-analysis requests, use the
specialized analysis tool that matches the request: use analyze_request for
FBA, pFBA, FVA, scenario comparison, pathway design, community analysis,
FSEOF, and strain design; use component inspection, knockout, essentiality,
and shadow-price tools for those operations. Never emulate a perturbation by
writing to the source model. For current weather questions, call the weather
tool with the requested location. When a web result suggests a source URL,
fetch it yourself before giving up. Explain briefly when done."""


def compile_reconstruction_request(
    prompt: str, workspace: Path, *, model: str | None = None
):
    """Compile a request through deterministic routing before any side effect."""
    return route_request(prompt, workspace, model=model)


def _preflight_result(
    prompt: str,
    model: str,
    decision: Any | None,
    *,
    route_error: Exception | None = None,
    event_sink: Any | None = None,
) -> AgentResult:
    """Return a reviewable result before constructing an LLM/tool graph."""
    if route_error is not None:
        status = "blocked_contract"
        reason = f"deterministic request validation failed: {route_error}"
        authorization: dict[str, object] = {}
    else:
        status = str(decision.status)
        reason = str(decision.reason)
        authorization = dict(decision.authorization or {})
    request_id = str(authorization.get("request_hash") or request_hash(prompt, model))
    approval_id = request_hash(f"{request_id}:{status}:{reason}", model)
    if decision is not None and status == "needs_approval":
        # The token returned to the caller must be the same workspace-bound
        # token accepted by ``continue_approved_route``.  Keeping a legacy
        # fallback for other preflight outcomes avoids changing their payload
        # contract while making approval continuation actually usable.
        approval_id = route_approval_id(decision, model)
    payload: dict[str, object] = {
        "status": status,
        "reason": reason,
        "request_hash": request_id,
        "approval_id": approval_id,
        "authorization": authorization,
    }
    if decision is not None and decision.contract is not None:
        payload["contract_id"] = decision.contract.contract_id
    if authorization.get("plan") is not None:
        payload["plan"] = authorization["plan"]
    event_name = "approval_required" if status == "needs_approval" else "request_blocked"
    if event_sink is not None:
        event_sink(event_name, payload)
    if status == "needs_approval":
        text = f"Approval required before execution: {reason}. Review approval_id={approval_id}."
    else:
        text = f"Request stopped before model execution ({status}): {reason}."
    return AgentResult(
        text=text,
        messages=[HumanMessage(content=prompt), AIMessage(content=text)],
        steps=0,
        stop_reason=status,
        token_usage_complete=True,
        cost_usage_complete=True,
    )


def build_langchain_model(config: AgentConfig) -> ChatModel:
    kwargs: dict[str, Any] = {"model": config.model, "temperature": 0}
    if config.protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if config.base_url:
            kwargs["base_url"] = config.base_url
        if config.api_key:
            kwargs["api_key"] = config.api_key
        return ChatAnthropic(**kwargs)
    if config.protocol == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if config.api_key:
            kwargs["google_api_key"] = config.api_key
        return ChatGoogleGenerativeAI(**kwargs)
    if config.base_url:
        kwargs["base_url"] = config.base_url
        kwargs["api_key"] = config.api_key or "not-needed"
        ensure_ollama_running(config.base_url, model=config.model)
    elif config.api_key:
        kwargs["api_key"] = config.api_key
    if config.model.startswith("deepseek"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["timeout"] = 120
    return ChatOpenAI(**kwargs)


def build_agent_tools(runner: LocalToolRunner, *, profile: str = "scientist") -> list[Any]:
    @tool
    def read_file(path: str, limit: int = 20_000) -> str:
        """Read a UTF-8 text file inside the workspace."""
        return runner.run("read_file", {"path": path, "limit": limit})

    @tool
    def write_file(path: str, content: str) -> str:
        """Write a UTF-8 text file inside the workspace."""
        return runner.run("write_file", {"path": path, "content": content})

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace exactly one text occurrence in a UTF-8 file."""
        return runner.run("edit_file", {"path": path, "old": old, "new": new})

    @tool
    def multi_edit(path: str, edits: list[dict[str, str]]) -> str:
        """Apply multiple exact text replacements to one UTF-8 file."""
        return runner.run("multi_edit", {"path": path, "edits": edits})

    @tool
    def list_files(path: str = ".", pattern: str = "*") -> str:
        """List files inside the workspace."""
        return runner.run("list_files", {"path": path, "pattern": pattern})

    @tool
    def grep(pattern: str, path: str = ".") -> str:
        """Search text inside workspace files."""
        return runner.run("grep", {"pattern": pattern, "path": path})

    @tool
    def run_shell(command: str) -> str:
        """Run a shell command in the workspace."""
        return runner.run("run_shell", {"command": command})

    @tool
    def run_powershell(command: str) -> str:
        """Run a PowerShell command in the workspace when PowerShell is installed."""
        return runner.run("run_powershell", {"command": command})

    @tool
    def web_fetch(url: str) -> str:
        """Fetch a web page by URL."""
        return runner.run("web_fetch", {"url": url})

    @tool
    def web_search(query: str) -> str:
        """Search the web and return readable result titles, URLs, and snippets."""
        return runner.run("web_search", {"query": query})

    @tool
    def weather(location: str) -> str:
        """Get current weather for a city or location from an online weather service."""
        return runner.run("weather", {"location": location})

    @tool
    def todo_write(items: list[dict[str, str]]) -> str:
        """Replace the session todo list."""
        return runner.run("todo_write", {"items": items})

    @tool
    def task_create(id: str, description: str) -> str:
        """Create a background task record."""
        return runner.run("task_create", {"id": id, "description": description})

    @tool
    def task_update(id: str, status: str, result: str | None = None) -> str:
        """Update a background task record."""
        return runner.run("task_update", {"id": id, "status": status, "result": result})

    @tool
    def task_list() -> str:
        """List background task records."""
        return runner.run("task_list", {})

    @tool
    def task_get(id: str) -> str:
        """Get one background task record."""
        return runner.run("task_get", {"id": id})

    @tool
    def memory_write(content: str) -> str:
        """Store a project memory in the current runtime state."""
        return runner.run("memory_write", {"content": content})

    @tool
    def list_mcp_resources() -> str:
        """List configured MCP resources and servers."""
        return runner.run("list_mcp_resources", {})

    @tool
    def read_mcp_resource(uri: str) -> str:
        """Read a simple MCP resource URI."""
        return runner.run("read_mcp_resource", {"uri": uri})

    @tool
    def agent(name: str, prompt: str) -> str:
        """Create a sub-agent task record."""
        return runner.run("agent", {"name": name, "prompt": prompt})

    @tool
    def team_create(team_name: str) -> str:
        """Create an agent team task record."""
        return runner.run("team_create", {"team_name": team_name})

    @tool
    def send_message(to: str, message: str) -> str:
        """Send a message to a named teammate mailbox."""
        return runner.run("send_message", {"to": to, "message": message})

    @tool
    def team_delete() -> str:
        """Delete the active team task records."""
        return runner.run("team_delete", {})

    @tool
    def metabolic_start(config_path: str) -> str:
        """Run FAA/FNA reconstruction and monitor it until a terminal job state.

        Use NCBI/PGAP annotation routes; the runtime polls metabolic_status
        internally and returns only after a terminal state.
        The default native engine maps evidence to the unified, quality-controlled
        BiGG/ModelSEED v6 library, selects a public biomass template using the
        library's declared DIAMOND/sketch policy, compiles AND/OR GPR rules,
        and gap-fills with a weighted LP.
        CarveMe/Reconstructor remain explicit legacy compatibility engines only.
        MEMOTE scoring runs by default; its score and extended test errors are separate
        from quality.json. Model files remain drafts requiring biological validation.
        """
        return _submit_reconstruction_and_wait(
            runner,
            {"config_path": config_path},
        )

    @tool
    def metabolic_batch_start(
        input_directory: str,
        output_directory: str,
        mapping: str,
        biomass_library: str,
        reaction_library: str,
        hmm_directory: str,
    ) -> str:
        """Build every mapped sequence and monitor the batch until it finishes."""
        return _submit_batch_and_wait(
            runner,
            {
                "input_directory": input_directory,
                "output_directory": output_directory,
                "mapping": mapping,
                "biomass_library": biomass_library,
                "reaction_library": reaction_library,
                "hmm_directory": hmm_directory,
            },
        )

    @tool
    def metabolic_batch_status(job_id: str) -> str:
        """Read the status of a submitted directory batch."""
        return runner.run("metabolic_batch_status", {"job_id": job_id})

    @tool
    def metabolic_status(job_id: str) -> str:
        """Read reconstruction job phase, results and quality status without restarting it."""
        return runner.run("metabolic_status", {"job_id": job_id})

    @tool
    def metabolic_resume(job_id: str) -> str:
        """Resume a recoverable job from a verified reaction-mapping checkpoint."""
        return runner.run("metabolic_resume", {"job_id": job_id})

    @tool
    def metabolic_cancel(
        job_id: str, terminate: bool = False, grace_seconds: float = 5.0
    ) -> str:
        """Request cancellation; optionally terminate the owned worker process group."""
        return runner.run(
            "metabolic_cancel",
            {
                "job_id": job_id,
                "terminate": terminate,
                "grace_seconds": grace_seconds,
            },
        )

    @tool
    def reconstruction_route(request: str) -> str:
        """Classify a reconstruction request without starting a job."""
        return runner.run("reconstruction_route", {"request": request})

    @tool
    def model_inspect(model_path: str) -> str:
        """Inspect an existing SBML model without modifying it."""
        return runner.run("model_inspect", {"model_path": model_path})

    @tool
    def inspect_model_component(
        model_path: str,
        component: str = "summary",
        identifier: str | None = None,
        limit: int = 100,
    ) -> str:
        """Inspect model summaries, reactions, metabolites, genes, exchanges, or compartments."""
        payload: dict[str, object] = {
            "model_path": model_path,
            "component": component,
            "limit": limit,
        }
        if identifier is not None:
            payload["identifier"] = identifier
        return runner.run("inspect_model_component", payload)

    @tool
    def simulate_knockouts(
        model_path: str,
        kind: str,
        identifiers: list[str],
        medium: dict[str, float] | None = None,
        objective_id: str | None = None,
    ) -> str:
        """Simulate independent gene or reaction knockouts without changing the source model."""
        payload: dict[str, object] = {
            "model_path": model_path,
            "kind": kind,
            "identifiers": identifiers,
        }
        if medium is not None:
            payload["medium"] = medium
        if objective_id is not None:
            payload["objective_id"] = objective_id
        return runner.run("simulate_knockouts", payload)

    @tool
    def scan_essentiality(
        model_path: str,
        kind: str = "gene",
        threshold: float = 0.1,
        max_items: int = 1000,
        medium: dict[str, float] | None = None,
        objective_id: str | None = None,
    ) -> str:
        """Scan single gene or reaction essentiality under explicit model conditions."""
        payload: dict[str, object] = {
            "model_path": model_path,
            "kind": kind,
            "threshold": threshold,
            "max_items": max_items,
        }
        if medium is not None:
            payload["medium"] = medium
        if objective_id is not None:
            payload["objective_id"] = objective_id
        return runner.run("scan_essentiality", payload)

    @tool
    def analyze_shadow_prices(
        model_path: str,
        medium: dict[str, float] | None = None,
        objective_id: str | None = None,
        limit: int = 5000,
    ) -> str:
        """Report dual shadow prices at the model optimum."""
        payload: dict[str, object] = {"model_path": model_path, "limit": limit}
        if medium is not None:
            payload["medium"] = medium
        if objective_id is not None:
            payload["objective_id"] = objective_id
        return runner.run("analyze_shadow_prices", payload)

    @tool
    def simulate_fba(model_path: str) -> str:
        """Run read-only FBA and report a model-condition prediction."""
        return runner.run("simulate_fba", {"model_path": model_path})

    @tool
    def simulate_pfba(model_path: str) -> str:
        """Run read-only parsimonious FBA and report a model-condition prediction."""
        return runner.run("simulate_pfba", {"model_path": model_path})

    @tool
    def simulate_fva(model_path: str, fraction_of_optimum: float = 1.0) -> str:
        """Run read-only FVA with an explicit optimum fraction."""
        return runner.run(
            "simulate_fva",
            {"model_path": model_path, "fraction_of_optimum": fraction_of_optimum},
        )

    @tool
    def compare_scenarios(
        model_path: str,
        scenarios: dict[str, dict[str, float]],
        objective_id: str | None = None,
        maintenance: float | None = None,
        maintenance_reaction_id: str | None = None,
        solver_tolerance: float | None = None,
    ) -> str:
        """Compare read-only FBA predictions across explicit medium scenarios."""
        payload = {"model_path": model_path, "scenarios": scenarios}
        for key, value in (
            ("objective_id", objective_id),
            ("maintenance", maintenance),
            ("maintenance_reaction_id", maintenance_reaction_id),
            ("solver_tolerance", solver_tolerance),
        ):
            if value is not None:
                payload[key] = value
        return runner.run(
            "compare_scenarios", payload
        )

    @tool
    def compare_models(models: dict[str, str]) -> str:
        """Compare read-only model metadata and hashes."""
        return runner.run("compare_models", {"models": models})

    @tool
    def render_report(result: dict[str, object]) -> str:
        """Render a structured, limitation-aware analysis report."""
        return runner.run("render_report", {"result": result})

    @tool
    def analyze_request(
        request: str,
        model_path: str,
        scenarios: dict[str, dict[str, float]] | None = None,
        network_model_path: str | None = None,
        substrate_id: str | None = None,
        product_id: str | None = None,
        number_of_optimizations: int = 10,
        min_fraction: float = 0.1,
        medium: dict[str, float] | None = None,
        maintenance: float | None = None,
        maintenance_reaction_id: str | None = None,
        solver_tolerance: float | None = None,
        biomass_id: str | None = None,
        objective_id: str | None = None,
        steps: int = 30,
        use_fva: bool = False,
        constrain_biomass: bool = False,
        max_flux_cutoff: float = 0.95,
        fraction_of_optimum: float = 1.0,
        community_models: dict[str, str] | None = None,
        strain_design_type: str | None = None,
        strain_design_config: dict[str, object] | None = None,
    ) -> str:
        """Run model analysis; QHEPath, StrainDesign, FSEOF, SMETANA and COBRA
        requests are routed by intent.
        """
        payload = {
            "request": request,
            "model_path": model_path,
        }
        if scenarios is not None:
            payload["scenarios"] = scenarios
        for key, value in (
            ("network_model_path", network_model_path),
            ("substrate_id", substrate_id),
            ("product_id", product_id),
            ("medium", medium),
            ("maintenance", maintenance),
            ("maintenance_reaction_id", maintenance_reaction_id),
            ("solver_tolerance", solver_tolerance),
            ("biomass_id", biomass_id),
            ("objective_id", objective_id),
            ("community_models", community_models),
            ("strain_design_type", strain_design_type),
            ("strain_design_config", strain_design_config),
        ):
            if value is not None:
                payload[key] = value
        payload["number_of_optimizations"] = number_of_optimizations
        payload["min_fraction"] = min_fraction
        payload["steps"] = steps
        payload["use_fva"] = use_fva
        payload["constrain_biomass"] = constrain_biomass
        payload["max_flux_cutoff"] = max_flux_cutoff
        payload["fraction_of_optimum"] = fraction_of_optimum
        return runner.run("analyze_request", payload)

    all_tools = [
        read_file,
        write_file,
        edit_file,
        multi_edit,
        list_files,
        grep,
        run_shell,
        run_powershell,
        web_fetch,
        web_search,
        weather,
        todo_write,
        task_create,
        task_update,
        task_list,
        task_get,
        memory_write,
        list_mcp_resources,
        read_mcp_resource,
        agent,
        team_create,
        send_message,
        team_delete,
        metabolic_start,
        metabolic_batch_start,
        metabolic_batch_status,
        metabolic_status,
        metabolic_resume,
        metabolic_cancel,
        reconstruction_route,
        model_inspect,
        inspect_model_component,
        simulate_knockouts,
        scan_essentiality,
        analyze_shadow_prices,
        simulate_fba,
        simulate_pfba,
        simulate_fva,
        compare_scenarios,
        compare_models,
        render_report,
        analyze_request,
    ]
    if profile == "developer":
        return all_tools
    if profile != "scientist":
        raise ValueError("tool profile must be scientist or developer")
    allowed = {"read_file", "list_files", "grep", *tool_spec_map()}
    return [item for item in all_tools if item.name in allowed]


def create_agent_graph(
    model: ChatModel,
    tools: list[Any],
    *,
    max_steps: int,
    max_tool_calls: int = 50,
    max_wall_seconds: float = 900.0,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_total_tokens: int | None = None,
    max_cost_usd: float | None = None,
    max_memory_mb: float | None = None,
    event_sink: Any | None = None,
) -> CompiledStateGraph:
    tools_by_name = {item.name: item for item in tools}
    model_with_tools = model.bind_tools(tools) if hasattr(model, "bind_tools") else model

    def call_model(state: AgentState) -> dict[str, object]:
        if event_sink is not None:
            event_sink("model_started", {"step": state["steps"]})
        memory_mb = _process_memory_mb()
        if max_memory_mb is not None and memory_mb > max_memory_mb:
            if event_sink is not None:
                event_sink(
                    "model_skipped",
                    {
                        "step": state["steps"],
                        "reason": "resource_budget_exhausted",
                        "memory_mb": memory_mb,
                    },
                )
            return {"messages": [], "stop_reason": "budget_exhausted"}
        response = model_with_tools.invoke(state["messages"])
        planned_calls = [
            str(call.get("name", "unknown"))
            for call in (getattr(response, "tool_calls", None) or [])
            if isinstance(call, dict)
        ]
        if event_sink is not None:
            # Do not expose arbitrary model prose as a chain-of-thought. The
            # trace reports only the selected tool names and deterministic
            # observations; this is enough to audit the ReAct transition.
            public_summary = "已选择工具并准备执行" if planned_calls else ""
            observed = None
            for message in reversed(state["messages"]):
                if isinstance(message, HumanMessage):
                    break
                if isinstance(message, ToolMessage):
                    observed = _trace_observation(message.content)
                    break
            event_sink(
                "execution_trace",
                {
                    "phase": "plan",
                    "step": state["steps"],
                    "action": planned_calls or ["respond"],
                    "summary": public_summary,
                    "based_on": observed,
                    "observation": {
                        "tool_calls_requested": planned_calls,
                    },
                    "next_action": "execute requested tools"
                    if planned_calls
                    else "return final response",
                },
            )
        usage = _extract_usage(response)
        input_tokens = state["input_tokens"] + (usage["input_tokens"] or 0)
        output_tokens = state["output_tokens"] + (usage["output_tokens"] or 0)
        total_tokens = state["total_tokens"] + (usage["total_tokens"] or 0)
        cost_usd = state["cost_usd"]
        if usage["cost_usd"] is not None:
            cost_usd = (cost_usd or 0.0) + usage["cost_usd"]
        usage_available = state["usage_available"] or usage["available"]
        token_usage_complete = state["token_usage_complete"] and all(
            usage[key] is not None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        cost_usage_complete = state["cost_usage_complete"] and usage["cost_usd"] is not None
        stop_reason = state.get("stop_reason")
        budget_exceeded = (
            (
                max_input_tokens is not None
                and usage["input_tokens"] is not None
                and input_tokens > max_input_tokens
            )
            or (
                max_output_tokens is not None
                and usage["output_tokens"] is not None
                and output_tokens > max_output_tokens
            )
            or (
                max_total_tokens is not None
                and usage["total_tokens"] is not None
                and total_tokens > max_total_tokens
            )
            or (max_cost_usd is not None and cost_usd is not None and cost_usd > max_cost_usd)
        )
        if budget_exceeded:
            stop_reason = "budget_exhausted"
        update = {
            "messages": [response],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "usage_available": usage_available,
            "token_usage_complete": token_usage_complete,
            "cost_usage_complete": cost_usage_complete,
            "stop_reason": stop_reason,
        }
        if event_sink is not None:
            event_sink(
                "model_finished",
                {
                    "step": state["steps"],
                    "usage_available": usage["available"],
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": usage["total_tokens"],
                    "cost_usd": usage["cost_usd"],
                    "budget_exhausted": budget_exceeded,
                },
            )
            snapshot = dict(state)
            snapshot.update(update)
            snapshot["messages"] = state["messages"] + [response]
            event_sink(
                "node_checkpoint",
                {
                    "node": "model",
                    "next_node": "tools" if getattr(response, "tool_calls", None) else "__end__",
                    "state": _snapshot_state(
                        snapshot,
                        "tools" if getattr(response, "tool_calls", None) else "__end__",
                    ),
                },
            )
        return update

    def run_tools(state: AgentState) -> dict[str, object]:
        last = state["messages"][-1]
        tool_messages: list[ToolMessage] = []
        tool_count = state["tool_calls"]
        stop_reason = state.get("stop_reason")
        for call in getattr(last, "tool_calls", []) or []:
            over_tool_budget = tool_count >= max_tool_calls
            over_wall_budget = time.monotonic() - state["started_at"] >= max_wall_seconds
            memory_mb = _process_memory_mb()
            over_resource_budget = max_memory_mb is not None and memory_mb > max_memory_mb
            if over_tool_budget or over_wall_budget or over_resource_budget:
                stop_reason = "budget_exhausted"
                content = "ToolError: budget_exhausted; tool call was not executed"
                tool_messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
                if event_sink is not None:
                    event_sink(
                        "tool_skipped",
                        {
                            "name": call["name"],
                            "tool_call_id": call["id"],
                            "reason": stop_reason,
                            "memory_mb": memory_mb,
                        },
                    )
                continue
            tool_count += 1
            selected_tool = tools_by_name.get(call["name"])
            if event_sink is not None:
                event_sink(
                    "tool_started",
                    {
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "input": _trace_input(call.get("args", {})),
                    },
                )
                event_sink(
                    "execution_trace",
                    {
                        "phase": "execute",
                        "step": state["steps"],
                        "action": call["name"],
                        "input": _trace_input(call.get("args", {})),
                        "next_action": "observe tool result",
                    },
                )
            if selected_tool is None:
                content = f"ToolError: unknown tool {call['name']}"
            else:
                try:
                    content = selected_tool.invoke(call["args"])
                except ToolError as error:
                    content = f"ToolError: {error}"
            tool_messages.append(ToolMessage(content=str(content), tool_call_id=call["id"]))
            if event_sink is not None:
                observation = _trace_observation(content)
                event_sink(
                    "tool_finished",
                    {
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "observation": observation,
                    },
                )
                event_sink(
                    "execution_trace",
                    {
                        "phase": "observe",
                        "step": state["steps"],
                        "action": call["name"],
                        "observation": observation,
                        "next_action": "model decides next step",
                    },
                )
                event_sink(
                    "job_progress",
                    {
                        "step": state["steps"],
                        "tool_calls": tool_count,
                        "completed_tool": call["name"],
                    },
                )
        update = {
            "messages": tool_messages,
            "steps": state["steps"] + 1,
            "tool_calls": tool_count,
            "stop_reason": stop_reason,
        }
        if event_sink is not None:
            snapshot = dict(state)
            snapshot.update(update)
            snapshot["messages"] = state["messages"] + tool_messages
            next_node = "__end__" if stop_reason else "model"
            event_sink(
                "node_checkpoint",
                {
                    "node": "tools",
                    "next_node": next_node,
                    "state": _snapshot_state(snapshot, next_node),
                },
            )
        return update

    def route(state: AgentState) -> str:
        if state.get("stop_reason") or state["steps"] >= max_steps:
            return END
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    def route_after_tools(state: AgentState) -> str:
        if state.get("stop_reason"):
            return END
        return "model"

    def start_route(state: AgentState) -> str:
        return state.get("resume_node") or "model"

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    graph.add_conditional_edges(
        START,
        start_route,
        {"model": "model", "tools": "tools", "__end__": END},
    )
    graph.add_conditional_edges("model", route, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", route_after_tools, {"model": "model", END: END})
    return graph.compile()


def run_agent(
    prompt: str,
    config: AgentConfig,
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    ask_permission: PermissionCallback | None = None,
    model: ChatModel | None = None,
    approval_id: str | None = None,
    inspection_result: dict[str, object] | None = None,
    execute_approved: bool = False,
    runtime_state: RuntimeState | None = None,
    hooks: dict[str, object] | None = None,
    images: list[Path] | None = None,
    prior_messages: list[BaseMessage] | None = None,
    command_env: dict[str, str] | None = None,
    additional_directories: list[Path] | None = None,
    respect_gitignore: bool = True,
    project_context: str | None = None,
    _event_sink: Any | None = None,
    _initial_state: AgentState | None = None,
) -> AgentResult:
    runtime_state = runtime_state or RuntimeState.for_workspace(config.workspace)

    def request_approval(tool_name: str, _tool_input: dict[str, object]) -> bool:
        if _event_sink is not None:
            _event_sink("approval_required", {"tool": tool_name})
        if ask_permission is None:
            return False
        approved = ask_permission(tool_name, _tool_input)
        if _event_sink is not None:
            _event_sink("approval_resolved", {"tool": tool_name, "approved": approved})
        return approved

    permission_callback = request_approval if ask_permission is not None else None
    runner = LocalToolRunner(
        workspace=config.workspace,
        mode=mode,
        ask_permission=permission_callback,
        shell_timeout=config.shell_timeout,
        runtime_state=runtime_state,
        file_history=FileHistory(config.workspace),
        mcp_registry=McpRegistry(config.workspace),
        hooks=hooks,
        request_id=request_hash(prompt, config.model),
        command_env=command_env,
        additional_directories=additional_directories,
        respect_gitignore=respect_gitignore,
        enforce_authorized_contract=config.tool_profile == "scientist",
    )
    if _event_sink is not None:
        def report_batch_progress(payload: dict[str, object]) -> None:
            _event_sink("batch_progress", payload)
            _event_sink(
                "execution_trace",
                {
                    "phase": "observe",
                    "action": "metabolic_batch_status",
                    "observation": _trace_progress(payload),
                    "next_action": (
                        "stop foreground monitoring; worker is not cancelled"
                        if payload.get("monitoring_interrupted")
                        else "continue monitoring worker"
                        if payload.get("status") not in {"completed", "failed", "cancelled"}
                        else "report terminal batch result"
                    ),
                },
            )

        def report_reconstruction_progress(payload: dict[str, object]) -> None:
            _event_sink("reconstruction_progress", payload)
            _event_sink(
                "execution_trace",
                {
                    "phase": "observe",
                    "action": "metabolic_status",
                    "observation": _trace_progress(payload),
                    "next_action": (
                        "stop foreground monitoring; worker is not cancelled"
                        if payload.get("monitoring_interrupted")
                        else "continue monitoring worker"
                        if payload.get("status") in _ACTIVE_RECONSTRUCTION_STATES
                        else "report reconstruction result or blocking evidence"
                    ),
                },
            )

        runner.batch_progress_callback = report_batch_progress
        runner.job_progress_callback = report_reconstruction_progress
    prepared_config_path: Path | None = None
    approved_continuation = False
    decision = None
    if config.tool_profile == "scientist":
        route_error: Exception | None = None
        try:
            decision = route_request(prompt, config.workspace, model=config.model)
        except (ContractError, ValueError) as error:
            decision = None
            route_error = error
        if _event_sink is not None:
            route_status = (
                str(decision.status)
                if decision is not None
                else "blocked_contract"
                if route_error is not None
                else "unknown"
            )
            route_tool = (
                str(decision.tool)
                if decision is not None and decision.tool
                else "preflight"
            )
            route_reason = (
                str(decision.reason)
                if decision is not None
                else str(route_error)
                if route_error is not None
                else "no route decision"
            )
            _event_sink(
                "execution_trace",
                {
                    "phase": "route",
                    "action": route_tool,
                    "observation": {
                        "status": route_status,
                        "reason": _trace_text(route_reason),
                    },
                    "next_action": (
                        "continue with authorized tool"
                        if decision is not None and decision.status in {"ready", "read_only"}
                        else "return deterministic validation result"
                    ),
                },
            )
        if route_error is not None or decision is None:
            return _preflight_result(
                prompt,
                config.model,
                decision,
                route_error=route_error,
                event_sink=_event_sink,
            )
        if decision.status == "needs_approval" and approval_id is not None:
            try:
                if inspection_result is None:
                    authorization = decision.authorization or {}
                    inspection_path = authorization.get("inspection_path")
                    if not isinstance(inspection_path, str) or not inspection_path:
                        raise ContractError("approval plan has no inspection path")
                    inspection_result = json.loads(
                        runner.run("model_inspect", {"model_path": inspection_path})
                    )
                    if not isinstance(inspection_result, dict):
                        raise ContractError("model_inspect returned a non-object result")
                    if _event_sink is not None:
                        _event_sink(
                            "inspection_completed",
                            {
                                "model_path": inspection_path,
                                "status": inspection_result.get("status"),
                                "model_sha256": inspection_result.get("model_sha256"),
                            },
                        )
                decision = continue_approved_route(
                    prompt,
                    config.workspace,
                    approval_id,
                    inspection_result,
                    model=config.model,
                )
                approved_continuation = True
            except (ContractError, ToolError, ValueError, TypeError, json.JSONDecodeError) as error:
                return _preflight_result(
                    prompt,
                    config.model,
                    decision,
                    route_error=error,
                    event_sink=_event_sink,
                )
        if decision.status not in {"ready", "read_only"}:
            return _preflight_result(prompt, config.model, decision, event_sink=_event_sink)
        if decision is not None and decision.status == "ready":
            # Job-control routes have no reconstruction contract, but their
            # authorized task/job binding is still required by the runner.
            runner.authorized_authorization = decision.authorization
            if decision.contract is not None:
                runner.authorized_contract_id = decision.contract.contract_id
                assert decision.config is not None
                # Route preparation already materializes this artifact.  Use
                # the same canonical identity check here so equivalent path
                # spellings (relative versus absolute) can reuse it safely.
                prepared_config_path = _materialize_route_config(
                    config.workspace, decision.contract, decision.config
                )
        if execute_approved and not approved_continuation:
            return _preflight_result(
                prompt,
                config.model,
                decision,
                route_error=ContractError(
                    "execute_approved requires a valid inspect-before-reconstruct approval"
                ),
                event_sink=_event_sink,
            )
    elif execute_approved:
        return _preflight_result(
            prompt,
            config.model,
            None,
            route_error=ContractError("execute_approved requires the scientist tool profile"),
            event_sink=_event_sink,
        )
    if execute_approved:
        assert prepared_config_path is not None
        config_path = str(prepared_config_path.relative_to(config.workspace))
        if _event_sink is not None:
            _event_sink(
                "tool_started",
                {"name": "metabolic_start", "approved_continuation": True},
            )
        previous_mode = runner.mode
        runner.mode = PermissionMode.AUTO
        try:
            completed = runner.run("metabolic_start", {"config_path": config_path})
        finally:
            runner.mode = previous_mode
        if _event_sink is not None:
            _event_sink(
                "tool_finished",
                {"name": "metabolic_start", "approved_continuation": True},
            )
            _event_sink(
                "job_progress",
                {"step": 0, "tool_calls": 1, "completed_tool": "metabolic_start"},
            )
        text = f"Approved reconstruction submitted: {completed}"
        return AgentResult(
            text=text,
            messages=[HumanMessage(content=prompt), AIMessage(content=text)],
            steps=1,
            stop_reason="submitted",
            token_usage_complete=True,
            cost_usage_complete=True,
        )
    tools = build_agent_tools(runner, profile=config.tool_profile)
    chat_model = model or build_langchain_model(config)
    graph = create_agent_graph(
        chat_model,
        tools,
        max_steps=config.max_steps,
        max_tool_calls=config.max_tool_calls,
        max_wall_seconds=config.max_wall_seconds,
        max_input_tokens=config.max_input_tokens,
        max_output_tokens=config.max_output_tokens,
        max_total_tokens=config.max_total_tokens,
        max_cost_usd=config.max_cost_usd,
        max_memory_mb=config.max_memory_mb,
        event_sink=_event_sink,
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT)]
    if (
        decision is not None
        and decision.status == "ready"
        and isinstance(decision.authorization, dict)
        and decision.authorization.get("status") == "batch_request_declared"
    ):
        authorization = decision.authorization
        messages.append(
            SystemMessage(
                content=(
                    "The user requested a directory batch reconstruction. The deterministic "
                    "route validated the assets. Call metabolic_batch_start exactly once with "
                    f"input_directory={authorization['input_directory']}, "
                    f"output_directory={authorization['output_target']}, "
                    f"mapping={authorization['mapping']}, "
                    f"biomass_library={authorization['biomass_library']}, "
                    f"reaction_library={authorization['reaction_library']}, "
                    f"hmm_directory={authorization['hmm_directory']}; wait for the tool to "
                    "return a terminal status, then report the final status, progress summary, "
                    "and any failure evidence. Do not call metabolic_batch_status in a loop."
                )
            )
        )
    if prepared_config_path is not None:
        messages.append(
            SystemMessage(
                content=(
                    "Validated reconstruction contract; use metabolic_start only with "
                    f"config_path={prepared_config_path}; the tool monitors the worker to a "
                    "terminal status. Do not call metabolic_status in a polling loop; after "
                    "completion, inspect the resulting model if the user requested metrics."
                )
            )
        )
    if (
        decision is not None
        and decision.status == "read_only"
        and isinstance(decision.authorization, dict)
        and decision.authorization.get("status")
        in {"model_inspection_request", "read_only_request"}
    ):
        authorization = decision.authorization
        model_path = authorization.get("input_path")
        if isinstance(model_path, str) and model_path:
            messages.append(
                SystemMessage(
                    content=(
                        "The deterministic route classified this as an existing model "
                        f"report request for model_path={model_path!r}. This is read-only: "
                        "never call metabolic_start, metabolic_batch_start, or any FASTA "
                        "reconstruction tool. Call model_inspect first, then use "
                        "inspect_model_component for genes, reactions, metabolites, and "
                        "other requested components; use simulate_fba or analyze_request "
                        "for model-condition growth. Report MEMOTE only from an observed "
                        "MEMOTE summary/report artifact and state clearly when it is absent, "
                        "skipped, or incomplete. Preserve the model path exactly."
                    )
                )
            )
    if (
        decision is not None
        and decision.status == "ready"
        and decision.tool in {"metabolic_status", "metabolic_batch_status"}
    ):
        authorization = decision.authorization or {}
        job_id = authorization.get("job_id")
        if isinstance(job_id, str) and job_id:
            status_tool = decision.tool
            messages.append(
                SystemMessage(
                    content=(
                        "This is a deterministic follow-up status request. Call "
                        f"{status_tool} exactly once with job_id={job_id!r}; do not "
                        "start or reconstruct another job. Report the observed job "
                        "status, phase, worker state, and result or failure evidence "
                        "from the tool response."
                    )
                )
            )
    if project_context:
        messages.append(SystemMessage(content=project_context))
    if prior_messages:
        started = time.perf_counter()
        compacted = _compact_messages(prior_messages, archive_root=config.workspace)
        messages.extend(compacted)
        if _event_sink is not None:
            _event_sink(
                "context_compacted",
                compaction_metrics(
                    prior_messages,
                    compacted,
                    (time.perf_counter() - started) * 1000,
                ),
            )
    messages.append(_build_user_message(prompt, images or []))
    initial_state = _initial_state or {
        "messages": messages,
        "steps": 0,
        "tool_calls": 0,
        "started_at": time.monotonic(),
        "stop_reason": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": None,
        "usage_available": False,
        "token_usage_complete": True,
        "cost_usage_complete": True,
        "resume_node": None,
    }
    final_state = graph.invoke(
        initial_state,
        config={"recursion_limit": max(10, config.max_steps * 3)},
    )
    messages = final_state["messages"]
    stop_reason = final_state.get("stop_reason") or (
        "budget_exhausted" if final_state["steps"] >= config.max_steps else "completed"
    )
    text = _last_assistant_text(messages)
    if not text:
        text = _tool_result_fallback(messages, final_state["steps"])
        if stop_reason == "budget_exhausted":
            text = "本轮达到执行预算上限；以下仅报告本轮已取得的证据。\n\n" + text
    return AgentResult(
        text=text,
        messages=messages,
        steps=final_state["steps"],
        stop_reason=stop_reason,
        input_tokens=final_state["input_tokens"],
        output_tokens=final_state["output_tokens"],
        total_tokens=final_state["total_tokens"],
        cost_usd=final_state["cost_usd"],
        token_usage_complete=final_state["token_usage_complete"],
        cost_usage_complete=final_state["cost_usage_complete"],
    )


def run_agent_stream(
    prompt: str,
    config: AgentConfig,
    *,
    mode: PermissionMode = PermissionMode.DEFAULT,
    model: ChatModel | None = None,
    ask_permission: PermissionCallback | None = None,
    approval_id: str | None = None,
    inspection_result: dict[str, object] | None = None,
    execute_approved: bool = False,
    runtime_state: RuntimeState | None = None,
    hooks: dict[str, object] | None = None,
    images: list[Path] | None = None,
    prior_messages: list[BaseMessage] | None = None,
    project_context: str | None = None,
    command_env: dict[str, str] | None = None,
    additional_directories: list[Path] | None = None,
    respect_gitignore: bool = True,
    on_event: Callable[[dict[str, object]], None] | None = None,
    thread_id: str | None = None,
    contract_id: str | None = None,
    event_store: EventStore | None = None,
    replay_after: int = 0,
    event_log_path: Path | None = None,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    sequence = 0
    if event_log_path is not None and event_log_path.exists():
        # ``sequence`` is scoped to a thread checkpoint, while the JSONL audit
        # log is shared by all runs. Continue after its last record so a later
        # CLI invocation does not fail the audit monotonicity check.
        try:
            lines = event_log_path.read_bytes().splitlines()
            if lines:
                previous = json.loads(lines[-1].decode("utf-8"))
                sequence = int(previous["sequence"])
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError("existing event log has an invalid sequence") from error

    checkpoint = None
    resume_state: AgentState | None = None
    if event_store is not None:
        if not thread_id:
            raise ValueError("thread_id is required when event_store is provided")
        checkpoint = event_store.begin(
            thread_id,
            request_hash(prompt, config.model),
            contract_id=contract_id,
        )
        all_events = event_store.events(thread_id)
        existing = event_store.events(thread_id, after_seq=replay_after)
        if checkpoint.status == "stopped":
            if on_event is not None:
                for item in existing:
                    on_event(item)
            return existing
        snapshot = event_store.latest_snapshot(thread_id)
        if snapshot is not None:
            started = {
                str(item.get("tool_call_id")): str(item.get("name"))
                for item in all_events
                if item.get("event") == "tool_started"
            }
            finished = {
                str(item.get("tool_call_id"))
                for item in all_events
                if item.get("event") in {"tool_finished", "tool_skipped"}
            }
            if any(
                tool_name in MUTATING_TOOLS and tool_id not in finished
                for tool_id, tool_name in started.items()
            ):
                if on_event is not None:
                    for item in existing:
                        on_event(item)
                return existing
            resume_state = _restore_state(snapshot["state"])
            # An interrupted local process may require explicit recovery.  Do
            # replay the durable prefix before continuing at the saved next node.
            if on_event is not None:
                for item in existing:
                    on_event(item)
        elif all_events:
            # An interrupted local process without a node snapshot requires
            # explicit recovery instead of risking duplicate side effects.
            if on_event is not None:
                for item in existing:
                    on_event(item)
            return existing
        sequence = max(
            sequence,
            max((int(item["seq"]) for item in all_events), default=0),
        )

    def emit(event: str, payload: dict[str, object]) -> None:
        nonlocal sequence
        sequence += 1
        item = {"seq": sequence, "ts": time.time(), "event": event, **payload}
        if checkpoint is not None:
            item["thread_id"] = checkpoint.thread_id
            item["attempt_id"] = checkpoint.attempt_id
        public_item = dict(item)
        if event_store is not None and thread_id is not None:
            event_store.append(thread_id, item)
        if event_log_path is not None:
            append_event(
                event_log_path,
                event,
                {
                    key: value
                    for key, value in public_item.items()
                    if key not in {"seq", "ts", "event", "state"}
                },
                sequence=sequence,
            )
        if event == "node_checkpoint":
            public_item.pop("state", None)
        events.append(public_item)
        if on_event is not None:
            on_event(public_item)

    emit("request_validated", {"model": config.model})
    emit(
        "execution_trace",
        {
            "phase": "accept",
            "action": "request received",
            "input": {"prompt": _trace_text(prompt)},
            "observation": {"model": config.model},
            "next_action": "validate route and select tools",
        },
    )
    try:
        result = run_agent(
            prompt,
            config,
            mode=mode,
            ask_permission=ask_permission,
            model=model,
            approval_id=approval_id,
            inspection_result=inspection_result,
            execute_approved=execute_approved,
            runtime_state=runtime_state,
            hooks=hooks,
            images=images,
            prior_messages=prior_messages,
            project_context=project_context,
            command_env=command_env,
            additional_directories=additional_directories,
            respect_gitignore=respect_gitignore,
            _event_sink=emit,
            _initial_state=resume_state,
        )
    except Exception as error:
        if isinstance(error, (TimeoutError, ConnectionError)):
            error_kind = "transient_provider"
            retryable = True
        elif isinstance(error, (ContractError, PermissionError, ValueError, TypeError)):
            error_kind = "constraint"
            retryable = False
        elif isinstance(error, ToolError):
            error_kind = "tool"
            retryable = False
        else:
            error_kind = "runtime"
            retryable = True
        emit(
            "run_failed",
            {
                "error_type": type(error).__name__,
                "error_kind": error_kind,
                "retryable": retryable,
            },
        )
        raise
    emit(
        "run_stopped",
        {
            "text": result.text,
            "steps": result.steps,
            "stop_reason": result.stop_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "cost_usd": result.cost_usd,
            "token_usage_complete": result.token_usage_complete,
            "cost_usage_complete": result.cost_usage_complete,
        },
    )
    if event_store is not None and thread_id is not None:
        event_store.finish(thread_id, result.stop_reason)
    return events


def _last_assistant_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, (HumanMessage, ToolMessage)):
            # Never reuse a previous turn's answer or a pre-tool plan as the
            # final answer when this turn exhausts its budget or returns empty.
            break
        if not isinstance(message, AIMessage):
            continue
        if message.tool_calls:
            continue
        content = message.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            blocks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            text = "".join(blocks).strip()
            if text:
                return text
    return ""


def _tool_result_fallback(messages: list[BaseMessage], steps: int) -> str:
    """Keep completed tool evidence visible when a provider emits an empty final turn."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if not isinstance(message, ToolMessage):
            continue
        content = str(message.content).strip()
        if not content:
            continue
        try:
            parsed: object = json.loads(content)
        except json.JSONDecodeError:
            evidence = content
        else:
            summary = _summarize_tool_payload(parsed)
            if summary:
                return summary
            evidence = json.dumps(parsed, ensure_ascii=False, indent=2, default=str)
        return (
            "工具已完成执行，但模型没有生成文字摘要。以下是工具返回的可核查结果：\n\n"
            f"{evidence[:20_000]}"
        )
    return f"已执行 {steps} 个工具步骤，但模型未返回最终文字。"


def _summarize_tool_payload(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    values = data if isinstance(data, dict) else payload
    if not all(key in values for key in ("reactions", "metabolites", "genes")):
        return None
    lines = [
        "模型检查完成：",
        f"- 反应数：{values['reactions']}",
        f"- 代谢物数：{values['metabolites']}",
        f"- 基因数：{values['genes']}",
    ]
    if values.get("objective") is not None:
        lines.append(f"- 目标函数：{values['objective']}")
    model_sha256 = payload.get("model_sha256") or values.get("model_sha256")
    if model_sha256:
        lines.append(f"- 模型 SHA256：{model_sha256}")
    return "\n".join(lines)


def _build_user_message(prompt: str, images: list[Path]) -> HumanMessage:
    if not images:
        return HumanMessage(content=prompt)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(image_content_block(path) for path in images)
    return HumanMessage(content=content)


def _compact_messages(
    messages: list[BaseMessage],
    keep_last: int = 40,
    *,
    archive_root: Path | None = None,
) -> list[BaseMessage]:
    if len(messages) <= keep_last:
        return messages
    serialized = [_serialize_message(message) for message in messages]
    encoded = json.dumps(serialized, ensure_ascii=False, sort_keys=True, default=str).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    archive_locator = f"archive://{digest}"
    if archive_root is not None:
        layout = RepoLayout(archive_root)
        archive = layout.writable(f".gemagents/context-archive/{digest}.json")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            archive.write_bytes(encoded)
        archive_locator = str(archive.relative_to(layout.root))

    groups: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in messages:
        if current and getattr(message, "type", None) == "human":
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    retained: list[BaseMessage] = []
    for group in reversed(groups):
        if retained and len(retained) + len(group) > keep_last:
            break
        retained[0:0] = group
    discarded = messages[: len(messages) - len(retained)]
    constraints = [
        str(message.content)[:1000]
        for message in discarded
        if getattr(message, "type", None) == "human"
    ]
    constraint_text = "\n".join(f"- {item}" for item in constraints[:20])
    summary_text = (
        f"Earlier context archived at {archive_locator}; sha256={digest}; "
        f"{len(discarded)} message(s) compacted. "
        "Tool transactions were retained as complete groups."
    )
    if constraint_text:
        summary_text += "\nArchived user constraints:\n" + constraint_text
    return [SystemMessage(content=summary_text), *retained]


def _serialize_message(message: BaseMessage) -> dict[str, object]:
    return {
        "type": getattr(message, "type", type(message).__name__),
        "content": message.content,
        "tool_calls": getattr(message, "tool_calls", []),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }


def _deserialize_message(payload: dict[str, object]) -> BaseMessage:
    message_type = payload.get("type")
    content = payload.get("content", "")
    if message_type == "human":
        return HumanMessage(content=content)
    if message_type == "system":
        return SystemMessage(content=content)
    if message_type == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=str(payload.get("tool_call_id", "")),
        )
    if message_type == "ai":
        return AIMessage(content=content, tool_calls=list(payload.get("tool_calls", [])))
    raise ValueError(f"unsupported checkpoint message type: {message_type}")


def _snapshot_state(state: dict[str, object], next_node: str) -> dict[str, object]:
    started_at = float(state["started_at"])
    return {
        "messages": [_serialize_message(message) for message in state["messages"]],
        "steps": int(state["steps"]),
        "tool_calls": int(state["tool_calls"]),
        "elapsed_seconds": max(0.0, time.monotonic() - started_at),
        "stop_reason": state.get("stop_reason"),
        "input_tokens": int(state.get("input_tokens", 0)),
        "output_tokens": int(state.get("output_tokens", 0)),
        "total_tokens": int(state.get("total_tokens", 0)),
        "cost_usd": state.get("cost_usd"),
        "usage_available": bool(state.get("usage_available", False)),
        "token_usage_complete": bool(state.get("token_usage_complete", False)),
        "cost_usage_complete": bool(state.get("cost_usage_complete", False)),
        "resume_node": next_node,
    }


def _restore_state(payload: object) -> AgentState:
    if not isinstance(payload, dict):
        raise ValueError("checkpoint state must be an object")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("checkpoint state messages must be a list")
    messages = [
        _deserialize_message(message)
        for message in raw_messages
        if isinstance(message, dict)
    ]
    if len(messages) != len(raw_messages):
        raise ValueError("checkpoint contains an invalid message")
    elapsed = float(payload.get("elapsed_seconds", 0.0))
    return {
        "messages": messages,
        "steps": int(payload.get("steps", 0)),
        "tool_calls": int(payload.get("tool_calls", 0)),
        "started_at": time.monotonic() - max(0.0, elapsed),
        "stop_reason": payload.get("stop_reason"),
        "input_tokens": int(payload.get("input_tokens", 0)),
        "output_tokens": int(payload.get("output_tokens", 0)),
        "total_tokens": int(payload.get("total_tokens", 0)),
        "cost_usd": payload.get("cost_usd"),
        "usage_available": bool(payload.get("usage_available", False)),
        "token_usage_complete": bool(payload.get("token_usage_complete", False)),
        "cost_usage_complete": bool(payload.get("cost_usage_complete", False)),
        "resume_node": payload.get("resume_node"),
    }
