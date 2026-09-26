"""Public biomass registry census and optional objective extraction."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import quote

import requests

from GemAgents.metabolic.io import metabolic_hash, metabolic_json


def metabolic_discover_public_biomass_registry(out: Path, download_models: bool = False) -> dict:
    """Record the public BiGG census and optionally extract objective biomass IDs."""
    response = requests.get("http://bigg.ucsd.edu/api/v2/models", timeout=(20, 60))
    response.raise_for_status()
    rows = response.json().get("results", [])
    records = []
    cache = out.parent / "biomass_registry_cache" / "bigg_models"
    if download_models:
        cache.mkdir(parents=True, exist_ok=True)
    for row in sorted(rows, key=lambda item: item.get("bigg_id", "")):
        model_id = row.get("bigg_id", "")
        if not model_id:
            continue
        record = {
            "id": model_id,
            "organism": row.get("organism", ""),
            "reaction_count": row.get("reaction_count"),
            "metabolite_count": row.get("metabolite_count"),
            "gene_count": row.get("gene_count"),
            "model_url": f"http://bigg.ucsd.edu/static/models/{quote(model_id)}.json",
            "details_url": f"http://bigg.ucsd.edu/api/v2/models/{quote(model_id)}",
            "biomass_status": "model_download_required_for_objective_extraction",
        }
        if download_models:
            model_path = cache / f"{model_id}.json.gz"
            if not model_path.is_file():
                model_response = requests.get(
                    f"http://bigg.ucsd.edu/static/models/{quote(model_id)}.json.gz",
                    timeout=(20, 120),
                )
                model_response.raise_for_status()
                model_path.write_bytes(model_response.content)
            try:
                with gzip.open(model_path, "rt", encoding="utf-8") as handle:
                    source_model = json.load(handle)
                objectives = [
                    reaction["id"]
                    for reaction in source_model.get("reactions", [])
                    if abs(float(reaction.get("objective_coefficient", 0))) > 1e-12
                ]
                record.update(
                    {
                        "model_file": str(model_path),
                        "model_sha256": metabolic_hash(model_path),
                        "biomass_reactions": objectives,
                        "biomass_status": "objective_extracted"
                        if len(objectives) == 1
                        else "ambiguous_or_missing_objective",
                    }
                )
            except (OSError, ValueError, KeyError) as error:
                record.update({"biomass_status": "model_parse_failed", "error": str(error)})
        records.append(record)
    result = {
        "schema_version": 1,
        "status": "complete",
        "source": "public BiGG Models API",
        "source_url": "http://bigg.ucsd.edu/api/v2/models",
        "models_count": len(records),
        "records": records,
        "scope": (
            "public model census; objective IDs extracted when download_models=true; "
            "sequence similarity still requires a reference genome/proteome"
        ),
    }
    metabolic_json(out, result)
    return result
