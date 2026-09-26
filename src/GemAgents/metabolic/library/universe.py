"""Deterministic reaction-universe selection and checksum validation."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.io import metabolic_hash
from GemAgents.metabolic.library.normalization import metabolic_normalize_ec_values


def metabolic_universe(config: dict):
    from cobra.io import read_sbml_model

    if config.get("reaction_library"):
        if config.get("universe"):
            raise ToolError("Choose reaction_library or universe, not both")
        directory = Path(config["reaction_library"])
        metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if config.get("engine", "carveme") == "carveme":
            filename = (
                "universe_bigg_full.xml.gz"
                if config.get("reaction_library_mode", "strict") == "full"
                else "universe_bigg.xml.gz"
            )
        else:
            filename = (
                "universe_full.xml.gz"
                if config.get("reaction_library_mode", "strict") == "full"
                else "universe.xml.gz"
            )
        path = directory / filename
        expected_hash = (
            metadata.get(
                "bigg_full_model_sha256"
                if filename == "universe_bigg_full.xml.gz"
                else "bigg_model_sha256"
            )
            if filename.startswith("universe_bigg")
            else metadata.get(
                "full_union_sha256" if filename == "universe_full.xml.gz" else "model_sha256"
            )
        )
        # Check the file explicitly before hashing so an incomplete library
        # consistently raises ToolError instead of leaking FileNotFoundError.
        if (
            metadata.get("status") != "complete"
            or not path.is_file()
            or not expected_hash
            or metabolic_hash(path) != expected_hash
        ):
            raise ToolError("Merged reaction library is incomplete or its checksum differs")
        applicable_kingdoms = metadata.get("applicable_kingdoms")
        if not isinstance(applicable_kingdoms, list) or not applicable_kingdoms:
            applicable_kingdoms = [metadata.get("kingdom")]
        if config.get("kingdom", "bacteria") not in applicable_kingdoms:
            raise ToolError("Merged library biomass does not match the requested kingdom")
        if config.get("gram", "unspecified") != metadata["gram"]:
            raise ToolError("Merged library biomass does not match the requested Gram template")
        model = read_sbml_model(str(path))
        model.solver = "glpk"
        model.solver.configuration.timeout = int(config.get("solver_timeout", 120))
        return model, path
    if config.get("universe"):
        path = Path(config["universe"])
    elif config.get("engine", "carveme") == "carveme":
        import carveme

        template = (
            "archaea"
            if config.get("kingdom") == "archaea"
            else {
                "negative": "gramneg",
                "positive": "grampos",
                "unspecified": "bacteria",
            }[config.get("gram", "unspecified")]
        )
        path = Path(carveme.project_dir) / f"data/generated/universe_{template}.xml.gz"
    else:
        from importlib.resources import files

        path = Path(str(files("reconstructor.resources").joinpath("universal.sbml.gz")))
    if not path.is_file():
        raise ToolError(f"Universal model not installed: {path}")
    model = read_sbml_model(str(path))
    model.solver = "glpk"
    model.solver.configuration.timeout = int(config.get("solver_timeout", 120))
    if config.get("engine") == "reconstructor":
        if config.get("gram") not in {"positive", "negative"}:
            raise ToolError(
                "Reconstructor requires an explicit Gram-positive/negative biomass choice"
            )
        model.objective = "biomass_GmPos" if config["gram"] == "positive" else "biomass_GmNeg"
        mapping = Path(config.get("modelseed_reactions", "data/ncbi_hmm/modelseed_reactions.tsv"))
        if not mapping.is_file():
            raise ToolError("Reconstructor EC mapping is missing; supply modelseed_reactions.tsv")
        with mapping.open(encoding="utf-8") as handle:
            ec_map = {
                r["id"]: metabolic_normalize_ec_values(r.get("ec_numbers", ""))
                for r in csv.DictReader(handle, delimiter="\t")
            }
        for reaction in model.reactions:
            ecs = ec_map.get(reaction.id.rsplit("_", 1)[0], [])
            if ecs:
                reaction.annotation["ec-code"] = ecs
    return model, path
