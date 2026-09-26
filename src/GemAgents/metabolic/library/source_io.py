"""Safe loading of public reaction-library source models."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.evidence import restricted_path


def load_source_model(path: Path):
    if restricted_path(path):
        raise ToolError("BLOCKED_POLICY: restricted source model")
    from cobra.io import load_json_model, model_from_dict, read_sbml_model

    if path.suffix.lower() == ".json":
        return load_json_model(str(path))
    if path.name.lower().endswith(".json.gz"):
        # BiGG publishes compressed JSON models.  Build directly from the
        # dictionary so we do not leave an uncompressed copy in the workspace.
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return model_from_dict(json.load(handle))
    return read_sbml_model(str(path))
