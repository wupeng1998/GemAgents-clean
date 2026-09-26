"""Public reconstruction facade over deterministic native-build leaves."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from GemAgents.metabolic.contracts import ReconstructionContext


def metabolic_native_build_model(
    universal,
    universe_path: Path,
    mapped: dict,
    evidence: list[dict],
    config: dict,
    out: Path,
    *,
    context: ReconstructionContext | None = None,
    choose_biomass_fn: Callable[..., tuple[dict, dict]] | None = None,
    add_reference_support_fn: Callable[..., tuple[set[str], dict, dict, dict]] | None = None,
    add_biomass_product_drains_fn: Callable[..., set[str]] | None = None,
    fasta_fn: Callable[..., list[tuple[str, str]]] | None = None,
    write_json_fn: Callable[..., None] | None = None,
    reference_growth_scenarios_fn: Callable[..., list[dict]] | None = None,
    oxygen_exchange_ids_fn: Callable[..., set[str]] | None = None,
    close_transport_fn: Callable[..., object] | None = None,
    biomass_precursor_audit_fn: Callable[..., dict] | None = None,
    evidence_status_fn: Callable[..., str] | None = None,
    solve_fn: Callable[..., object] | None = None,
):
    """Build a native model while keeping historical callback seams injectable."""
    from GemAgents import solver_result
    from GemAgents.metabolic.biomass.selection import choose_biomass
    from GemAgents.metabolic.evidence.mapping import metabolic_evidence_status
    from GemAgents.metabolic.io import metabolic_json
    from GemAgents.metabolic.reconstruction.initial_model import close_transport_and_annotate
    from GemAgents.metabolic.reconstruction.native_build import (
        metabolic_native_build_model as _native_build,
    )
    from GemAgents.metabolic.reconstruction.precursor_audit import (
        metabolic_biomass_precursor_audit,
    )
    from GemAgents.metabolic.reconstruction.reference_bridge import (
        add_reference_iml1515_support,
    )
    from GemAgents.metabolic.reconstruction.support import (
        add_biomass_product_drains,
        oxygen_exchange_ids,
        reference_growth_scenarios,
    )

    # The sequence leaf lives one package level above reconstruction.  Keep
    # the import local so importing this facade remains provider/asset free.
    if fasta_fn is None:
        from GemAgents.metabolic.sequence import metabolic_fasta

        fasta_fn = metabolic_fasta
    return _native_build(
        universal,
        universe_path,
        mapped,
        evidence,
        config,
        out,
        context=context,
        choose_biomass_fn=choose_biomass if choose_biomass_fn is None else choose_biomass_fn,
        add_reference_support_fn=(
            add_reference_iml1515_support
            if add_reference_support_fn is None
            else add_reference_support_fn
        ),
        add_biomass_product_drains_fn=(
            add_biomass_product_drains
            if add_biomass_product_drains_fn is None
            else add_biomass_product_drains_fn
        ),
        fasta_fn=fasta_fn,
        write_json_fn=metabolic_json if write_json_fn is None else write_json_fn,
        reference_growth_scenarios_fn=(
            reference_growth_scenarios
            if reference_growth_scenarios_fn is None
            else reference_growth_scenarios_fn
        ),
        oxygen_exchange_ids_fn=(
            oxygen_exchange_ids
            if oxygen_exchange_ids_fn is None
            else oxygen_exchange_ids_fn
        ),
        close_transport_fn=(
            close_transport_and_annotate if close_transport_fn is None else close_transport_fn
        ),
        biomass_precursor_audit_fn=(
            metabolic_biomass_precursor_audit
            if biomass_precursor_audit_fn is None
            else biomass_precursor_audit_fn
        ),
        evidence_status_fn=(
            metabolic_evidence_status if evidence_status_fn is None else evidence_status_fn
        ),
        solve_fn=solver_result.solve_with_classification if solve_fn is None else solve_fn,
    )


__all__ = ["metabolic_native_build_model"]
