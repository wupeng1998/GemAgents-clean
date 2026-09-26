from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from GemAgents.layout import resolve_workspace


@dataclass(frozen=True)
class AgentConfig:
    workspace: Path
    model: str
    protocol: str = "openai-chat"
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    max_steps: int = 20
    max_tool_calls: int = 50
    max_wall_seconds: float = 900.0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_memory_mb: float | None = None
    shell_timeout: int = 30
    tool_profile: str = "scientist"


def load_config(
    *,
    workspace: str | None = None,
    model: str | None = None,
    protocol: str = "openai-chat",
    base_url: str | None = None,
    api_key: str | None = None,
    max_steps: int = 20,
    max_tool_calls: int = 50,
    max_wall_seconds: float = 900.0,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_total_tokens: int | None = None,
    max_cost_usd: float | None = None,
    max_memory_mb: float | None = None,
    shell_timeout: int = 30,
    tool_profile: str = "scientist",
) -> AgentConfig:
    if tool_profile not in {"scientist", "developer"}:
        raise ValueError("tool_profile must be scientist or developer")
    budgets = (max_input_tokens, max_output_tokens, max_total_tokens)
    if max_steps < 1 or max_tool_calls < 1 or max_wall_seconds <= 0 or any(
        value is not None and value <= 0 for value in budgets
    ) or (max_cost_usd is not None and max_cost_usd <= 0) or (
        max_memory_mb is not None and max_memory_mb <= 0
    ):
        raise ValueError("step, tool-call and wall budgets must be positive")
    model_name = (
        model
        or os.getenv("GEMAGENTS_MODEL")
        or os.getenv("OLLAMA_MODEL")
        or ("deepseek-v4-flash" if deepseek_api_key() else None)
    )
    if not model_name:
        raise ValueError("Set --model, GEMAGENTS_MODEL, or OLLAMA_MODEL before running the agent.")

    workspace_path = resolve_workspace(workspace)
    ollama_base_url = _ollama_base_url() if _uses_local_ollama(model_name) else None
    is_deepseek = model_name.startswith("deepseek")
    if model_name == "deepseek":
        model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    return AgentConfig(
        workspace=workspace_path,
        model=model_name,
        protocol=protocol,
        base_url=base_url
        or (
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            if is_deepseek
            else os.getenv("OPENAI_BASE_URL") or ollama_base_url
        ),
        api_key=api_key or (deepseek_api_key() if is_deepseek else os.getenv("OPENAI_API_KEY")),
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        max_wall_seconds=max_wall_seconds,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_total_tokens=max_total_tokens,
        max_cost_usd=max_cost_usd,
        max_memory_mb=max_memory_mb,
        shell_timeout=shell_timeout,
        tool_profile=tool_profile,
    )


def deepseek_api_key() -> str | None:
    """Support the standard variable and the user's historical DEEPEEK spelling."""
    return os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPEEK_API_KEY")


def _ollama_base_url() -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"


_LOCAL_OLLAMA_PREFIXES = ("qwen", "llama", "mistral", "mixtral", "gemma", "phi", "llava")


def _uses_local_ollama(model_name: str) -> bool:
    """Recognize explicit local-model names without hijacking cloud models."""
    configured = os.getenv("OLLAMA_MODEL")
    return bool(os.getenv("OLLAMA_BASE_URL")) and (
        model_name == configured
        or model_name.casefold().startswith(_LOCAL_OLLAMA_PREFIXES)
    )
