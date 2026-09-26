from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from GemAgents.datetime_compat import StrEnum
from GemAgents.metabolic.jobs.contracts import tool_spec_map


class PermissionMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"
    AUTO = "auto"


_READ_ONLY_BUILTINS = {
    "read_file",
    "list_files",
    "grep",
    "web_fetch",
    "web_search",
    "weather",
    "task_list",
    "task_get",
    "list_mcp_resources",
    "read_mcp_resource",
    "metabolic_status",
    "metabolic_batch_status",
    "model_inspect",
    "simulate_fba",
    "simulate_fva",
    "compare_scenarios",
    "compare_models",
    "render_report",
    "analyze_request",
}
_MUTATING_BUILTINS = {
    "write_file",
    "edit_file",
    "multi_edit",
    "run_shell",
    "run_powershell",
    "todo_write",
    "task_create",
    "task_update",
    "memory_write",
    "restore_file",
    "agent",
    "team_create",
    "team_delete",
    "send_message",
    "metabolic_start",
    "metabolic_batch_start",
    "metabolic_resume",
    "metabolic_cancel",
    "reconstruction_route",
}

_DOMAIN_TOOL_SPECS = tool_spec_map()
_DOMAIN_READ_ONLY = {
    name for name, spec in _DOMAIN_TOOL_SPECS.items() if not spec.side_effects
}
_DOMAIN_MUTATING = {
    name for name, spec in _DOMAIN_TOOL_SPECS.items() if spec.side_effects
}
if _DOMAIN_READ_ONLY & _DOMAIN_MUTATING:
    raise RuntimeError("domain tool permission registry has overlapping classifications")
if (_DOMAIN_READ_ONLY | _DOMAIN_MUTATING) != set(_DOMAIN_TOOL_SPECS):
    raise RuntimeError("domain tool permission registry has unclassified tools")

READ_ONLY_TOOLS = frozenset(_READ_ONLY_BUILTINS | _DOMAIN_READ_ONLY)
MUTATING_TOOLS = frozenset(_MUTATING_BUILTINS | _DOMAIN_MUTATING)


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str


PermissionCallback = Callable[[str, dict[str, object]], bool]


def decide_permission(
    tool_name: str,
    tool_input: dict[str, object],
    *,
    mode: PermissionMode,
    ask: PermissionCallback | None = None,
) -> PermissionDecision:
    if tool_name in READ_ONLY_TOOLS:
        return PermissionDecision(True, "read-only tool")

    if tool_name not in MUTATING_TOOLS:
        return PermissionDecision(False, f"unknown tool: {tool_name}")

    if mode is PermissionMode.PLAN:
        return PermissionDecision(False, "plan mode allows read-only tools only")

    if mode is PermissionMode.AUTO:
        return PermissionDecision(True, "auto mode")

    if ask is None:
        return PermissionDecision(False, "non-interactive default mode denied mutation")

    if ask(tool_name, tool_input):
        return PermissionDecision(True, "approved by user")

    return PermissionDecision(False, "rejected by user")
