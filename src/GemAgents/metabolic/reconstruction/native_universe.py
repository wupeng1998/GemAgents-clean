"""Native shared prokaryotic reaction-universe loading for reconstruction."""

from __future__ import annotations

import json
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash
from GemAgents.metabolic.library.quality import (
    apply_canonical_chemistry,
    guard_energy_hydrolysis_direction,
)


def normalize_bigg_compartments(model) -> int:
    """Restore compartment metadata lost by flattened multi-model SBML files."""
    known = {
        "c",
        "e",
        "p",
        "m",
        "r",
        "h",
        "x",
        "g",
        "l",
        "n",
        "u",
        "v",
        "s",
        "w",
        "f",
        "y",
        "cx",
        "um",
        "cm",
        "mm",
        "im",
        "i",
    }
    changed = 0
    for metabolite in model.metabolites:
        suffix = metabolite.id.rsplit("_", 1)[-1]
        if suffix in known and metabolite.compartment != suffix:
            metabolite.compartment = suffix
            changed += 1
    if changed:
        model.notes["normalized_bigg_compartments"] = str(changed)
    return changed

def metabolic_native_universe(config: dict):
    """Load the operational shared-prokaryotic SBML view without CarveMe or Reconstructor."""
    from cobra.io import read_sbml_model

    directory = Path(config.get("reaction_library", ""))
    if not directory.is_dir():
        raise ToolError("Native reconstruction requires a reaction_library directory")
    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    full = config.get("reaction_library_mode", "strict") == "full"
    path = directory / ("universe_full.xml.gz" if full else "universe.xml.gz")
    expected_hash = metadata.get("full_union_sha256" if full else "model_sha256")
    if (
        metadata.get("status") != "complete"
        or not path.is_file()
        or not expected_hash
        or metabolic_hash(path) != expected_hash
    ):
        raise ToolError("Reaction library is incomplete or its checksum differs")
    requested_kingdom = config.get("kingdom", "bacteria")
    library_kingdom = metadata.get("kingdom")
    applicable_kingdoms = metadata.get("applicable_kingdoms")
    if not isinstance(applicable_kingdoms, list) or not applicable_kingdoms:
        applicable_kingdoms = [library_kingdom]
    if requested_kingdom not in applicable_kingdoms:
        raise ToolError(
            "Reaction library taxonomy does not match the requested kingdom: "
            f"library={library_kingdom!r}, requested={requested_kingdom!r}"
        )
    requested_gram = config.get("gram", "unspecified")
    library_gram = metadata.get("gram")
    if (
        library_gram
        and library_gram != "unspecified"
        and requested_gram != library_gram
    ):
        raise ToolError(
            "Reaction library taxonomy does not match the requested Gram type: "
            f"library={library_gram!r}, requested={requested_gram!r}"
        )
    model = read_sbml_model(str(path))
    if requested_kingdom != library_kingdom:
        model.notes["taxonomy_compatibility"] = "shared_prokaryotic_library"
        model.notes["reaction_library_source_kingdom"] = str(library_kingdom)
    normalize_bigg_compartments(model)
    chemistry_corrections = apply_canonical_chemistry(model)
    if chemistry_corrections:
        model.notes["qc_canonical_chemistry_corrections"] = str(
            len(chemistry_corrections)
        )
    guarded = sum(guard_energy_hydrolysis_direction(reaction) for reaction in model.reactions)
    if guarded:
        model.notes["qc_energy_direction_guards"] = str(guarded)
    model.solver = "glpk"
    model.solver.configuration.timeout = int(config.get("solver_timeout", 120))
    return model, path
