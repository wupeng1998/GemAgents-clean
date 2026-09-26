"""Conservative classification of empirical biomass assembly reactions."""

from __future__ import annotations

import math
import re


def is_empirical_biomass_assembly(reaction, biomass_reactants: set[str]) -> bool:
    """Identify direct empirical pool assembly, not merely a no-GPR enzyme."""
    products = {
        metabolite.id for metabolite, coefficient in reaction.metabolites.items() if coefficient > 0
    }
    if not products.intersection(biomass_reactants):
        return False
    # Public BiGG models sometimes encode an explicit biomass pool assembly as
    # ``BIOMASS_*`` even when it spans compartments or carries a source GPR.
    # It is still a declared empirical pseudoreaction: retain it as support
    # with its source identity instead of treating its pool as an unmapped
    # metabolite.  The product/objective intersection above prevents a broad
    # prefix match from admitting unrelated reactions.
    explicit_source_assembly = str(reaction.id).casefold().startswith("biomass_")
    generic_products = {
        "h2o_c",
        "h_c",
        "atp_c",
        "adp_c",
        "gtp_c",
        "gdp_c",
        "pi_c",
        "ppi_c",
        "amp_c",
        "cmp_c",
        "ump_c",
        "udp_c",
    }
    source_pool_products = products.intersection(biomass_reactants) - generic_products
    if explicit_source_assembly:
        return bool(source_pool_products)
    if reaction.boundary or reaction.gene_reaction_rule:
        return False
    if len({metabolite.compartment for metabolite in reaction.metabolites}) != 1:
        return False
    tokens = set(
        re.findall(
            r"[a-z0-9]+",
            f"{reaction.id} {reaction.name or ''}".casefold(),
        )
    )
    empirical_markers = {
        "ass",
        "assembly",
        "biomass",
        "dna",
        "ion",
        "ions",
        "lipid",
        "pool",
        "protein",
        "rna",
    }
    if not tokens.intersection(empirical_markers):
        return False
    try:
        residual = reaction.check_mass_balance()
    except (TypeError, ValueError, AttributeError):
        return True
    return bool(residual)

def is_empirical_pool_reaction(reaction, required_products: set[str]) -> bool:
    """Recognize unbalanced empirical-pool chemistry upstream of biomass."""
    if reaction.boundary or reaction.gene_reaction_rule:
        return False
    if not any(
        coefficient > 0 and metabolite.id in required_products
        for metabolite, coefficient in reaction.metabolites.items()
    ):
        return False
    if len({metabolite.compartment for metabolite in reaction.metabolites}) != 1:
        return False
    try:
        if not reaction.check_mass_balance():
            return False
    except (TypeError, ValueError, AttributeError):
        pass
    fractional_composition = any(
        not math.isclose(abs(float(coefficient)), round(abs(float(coefficient))), abs_tol=1e-9)
        for coefficient in reaction.metabolites.values()
    )
    empirical_formula = False
    for metabolite in reaction.metabolites:
        try:
            elements = metabolite.elements or {}
            empirical_formula |= any(abs(float(count)) >= 100 for count in elements.values())
        except (TypeError, ValueError, AttributeError):
            continue
    tokens = set(
        re.findall(
            r"[a-z0-9]+",
            f"{reaction.id} {reaction.name or ''}".casefold(),
        )
    )
    label = bool(
        tokens
        & {
            "ass",
            "assembly",
            "biomass",
            "free",
            "lipid",
            "mycolicacid",
            "pool",
        }
    )
    return fractional_composition or empirical_formula or label
