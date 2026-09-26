"""Read-only SMETANA-compatible community interaction analysis.

The upstream SMETANA project focuses on stoichiometric cross-feeding in a
collection of genome-scale models.  This adapter keeps that analysis available
inside GemAgents without making the agent import an optional command-line
package at module import time.  COBRApy performs the model loading and FVA;
the returned interaction candidates are deliberately labelled as model
potentials rather than observations of a microbial community.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path):
    from cobra.io import load_json_model, read_sbml_model

    if not path.is_file():
        raise FileNotFoundError(path)
    lowered = path.name.casefold()
    if lowered.endswith(".json") or lowered.endswith(".json.gz"):
        return load_json_model(str(path))
    return read_sbml_model(str(path))


def _validate_medium(medium: dict[str, float] | None) -> None:
    if medium is None:
        return
    if not isinstance(medium, dict):
        raise ValueError("medium must be an object")
    for reaction_id, bound in medium.items():
        if not isinstance(reaction_id, str) or not reaction_id.strip():
            raise ValueError("medium reaction IDs must be non-empty strings")
        if type(bound) not in {int, float} or not math.isfinite(float(bound)):
            raise ValueError("medium bounds must be finite numbers")


def _metabolite_key(reaction) -> str | None:
    metabolites = list(reaction.metabolites)
    if len(metabolites) != 1:
        return None
    metabolite = metabolites[0]
    compartment = str(getattr(metabolite, "compartment", "") or "").casefold()
    identifier = str(metabolite.id)
    if compartment and compartment not in {"e", "extracellular", "external"}:
        if not identifier.casefold().endswith(("_e", "[e]")):
            return None
    # Exchange IDs commonly carry a compartment suffix (for example ``_e``);
    # matching the base ID makes models from different reconstruction tools
    # interoperable while retaining the original ID in the result.
    for suffix in ("_e", "_c", "[e]", "[c]"):
        if identifier.endswith(suffix):
            return identifier[: -len(suffix)]
    return identifier


def _exchange_potentials(model_path: Path, medium: dict[str, float] | None, fraction: float):
    from cobra.flux_analysis import flux_variability_analysis

    _validate_medium(medium)
    model = _load(model_path).copy()
    if medium is not None:
        model.medium = dict(medium)
    exchanges = [reaction for reaction in model.reactions if reaction.boundary]
    if not exchanges:
        return {}
    table = flux_variability_analysis(
        model,
        reaction_list=exchanges,
        fraction_of_optimum=fraction,
    )
    potentials: dict[str, dict[str, object]] = {}
    for reaction in exchanges:
        key = _metabolite_key(reaction)
        if key is None:
            continue
        row = table.loc[reaction.id]
        minimum = float(row.minimum)
        maximum = float(row.maximum)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum > maximum:
            raise ValueError(
                f"SMETANA exchange screening received invalid solver bounds for {reaction.id}"
            )
        entry = potentials.setdefault(
            key,
            {"metabolite_id": key, "reactions": [], "uptake": 0.0, "secretion": 0.0},
        )
        entry["reactions"].append(reaction.id)
        entry["uptake"] = max(float(entry["uptake"]), max(0.0, -minimum))
        entry["secretion"] = max(float(entry["secretion"]), max(0.0, maximum))
    return potentials


def analyze_community(
    community_models: dict[str, Path],
    *,
    medium: dict[str, float] | None = None,
    fraction_of_optimum: float = 1.0,
    tolerance: float = 1e-9,
) -> dict[str, object]:
    """Calculate pairwise cross-feeding potentials for a model collection."""
    if not isinstance(community_models, dict) or len(community_models) < 2:
        raise ValueError("SMETANA requires at least two named community models")
    if not 0 < fraction_of_optimum <= 1:
        raise ValueError("fraction_of_optimum must be in (0, 1]")
    if tolerance <= 0 or not math.isfinite(tolerance):
        raise ValueError("tolerance must be a positive finite number")

    potentials: dict[str, dict[str, dict[str, object]]] = {}
    hashes: dict[str, str] = {}
    for name, path in community_models.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("community model names must be non-empty strings")
        model_path = Path(path)
        hashes[name] = _hash(model_path)
        potentials[name] = _exchange_potentials(model_path, medium, fraction_of_optimum)

    interactions: list[dict[str, object]] = []
    contribution: dict[str, float] = {name: 0.0 for name in community_models}
    overlap: dict[str, int] = {}
    names = list(community_models)
    for donor in names:
        for recipient in names:
            if donor == recipient:
                continue
            shared = set(potentials[donor]) & set(potentials[recipient])
            for metabolite in sorted(shared):
                donor_capacity = float(potentials[donor][metabolite]["secretion"])
                recipient_capacity = float(potentials[recipient][metabolite]["uptake"])
                if donor_capacity <= tolerance or recipient_capacity <= tolerance:
                    continue
                score = min(donor_capacity, recipient_capacity)
                interactions.append(
                    {
                        "donor": donor,
                        "recipient": recipient,
                        "metabolite": metabolite,
                        "donor_capacity": donor_capacity,
                        "recipient_uptake_capacity": recipient_capacity,
                        "interaction_score": score,
                    }
                )
                contribution[donor] += score
                overlap[metabolite] = overlap.get(metabolite, 0) + 1

    return {
        "status": "completed",
        "track": "pairwise_exchange_screen",
        "method": "pairwise_exchange_screen",
        "solver_status": "optimality_constrained",
        "formal_smetana_metrics": "not_computed",
        "upstream": "https://github.com/cdanielmachado/smetana",
        "backend": "cobra-compatible community adapter",
        "community_model_sha256": hashes,
        "members": names,
        "fraction_of_optimum": fraction_of_optimum,
        "interactions": interactions,
        "metrics": {
            "exchange_pair_candidates": len(interactions),
            "pairwise_exchange_score": sum(
                float(item["interaction_score"]) for item in interactions
            ),
            "species_exchange_capacity": contribution,
            "shared_exchange_metabolites": overlap,
        },
        "limitations": [
            "Pairwise exchange candidates are stoichiometric model potentials, not evidence "
            "of coexistence, metabolite transfer, or measured community fitness.",
            "This route does not compute formal SMETANA metrics; its pairwise screen "
            "must not be reported as a SMETANA score.",
            "Exchange direction and capacity depend on each model's bounds, medium, "
            "objective, and solver assumptions.",
        ],
    }
