"""Reference-model comparison metadata for completed reconstruction runs."""

from __future__ import annotations

import json
from pathlib import Path

from GemAgents.errors import ToolError


def record_reference_comparison(
    *,
    config: dict,
    out: Path,
    model,
    quality: dict,
    manifest: dict,
    load_source_model,
) -> None:
    """Record reference/model gene-count parity when a declared source exists."""
    reference_gene_count = None
    biomass_metadata = json.loads((out / "biomass-selection.json").read_text(encoding="utf-8"))
    selected_template = str(biomass_metadata.get("selected_template", "public_reference"))
    reference_value = config.get("reference_support_path") or biomass_metadata.get(
        "template", {}
    ).get("model")
    reference_path = Path(str(reference_value)) if reference_value else Path("")
    if reference_path.is_file():
        try:
            reference_gene_count = len(load_source_model(reference_path).genes)
        except (OSError, TypeError, ValueError, ToolError):
            manifest.update(
                {
                    key: quality[key]
                    for key in (
                        "validation_status",
                        "benchmark_eligibility",
                        "repair_search_status",
                        "final_audit_status",
                    )
                }
            )
    if reference_gene_count is not None:
        manifest["reference_comparison"] = {
            "model": selected_template,
            "reference_genes": reference_gene_count,
            "model_genes": len(model.genes),
            "delta": len(model.genes) - reference_gene_count,
            "parity": len(model.genes) >= reference_gene_count,
        }
