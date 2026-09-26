"""Biomass contracts and public biomass library entry points."""

from GemAgents.metabolic.biomass.assembly import (
    is_empirical_biomass_assembly,
    is_empirical_pool_reaction,
)
from GemAgents.metabolic.biomass.catalog import build_metabolite_index, map_biomass_stoichiometry
from GemAgents.metabolic.biomass.contracts import (
    BiomassContract,
    classify_biomass_gap,
    required_precursors,
)
from GemAgents.metabolic.biomass.reference import reference_proteins
from GemAgents.metabolic.biomass.registry import metabolic_discover_public_biomass_registry
from GemAgents.metabolic.biomass.selection import choose_biomass, sequence_sketch, sketch_similarity

__all__ = [
    "BiomassContract",
    "build_metabolite_index",
    "choose_biomass",
    "classify_biomass_gap",
    "is_empirical_biomass_assembly",
    "is_empirical_pool_reaction",
    "metabolic_discover_public_biomass_registry",
    "metabolic_prepare_biomass_library",
    "map_biomass_stoichiometry",
    "required_precursors",
    "reference_proteins",
    "sequence_sketch",
    "sketch_similarity",
]


def __getattr__(name: str):
    if name in {
        "metabolic_prepare_biomass_library",
    }:
        from GemAgents.metabolic import legacy

        return getattr(legacy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
