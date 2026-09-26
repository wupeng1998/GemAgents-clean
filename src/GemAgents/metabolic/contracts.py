"""Public metabolic configuration and status contracts."""

from GemAgents.contracts import (
    STRING_OPTIONS,
    ReconstructionConfig,
    ReconstructionContext,
    apply_reconstruction_defaults,
    load_configuration,
    quality_status_contract,
)
from GemAgents.errors import ToolError


def metabolic_validate_options(config: dict) -> None:
    """Validate reconstruction options at the deterministic API boundary."""
    try:
        ReconstructionConfig(config)
    except ValueError as error:
        raise ToolError(str(error)) from error

__all__ = [
    "ReconstructionConfig",
    "ReconstructionContext",
    "STRING_OPTIONS",
    "apply_reconstruction_defaults",
    "load_configuration",
    "metabolic_validate_options",
    "quality_status_contract",
]
