"""Deterministic model-quality checks used after reconstruction."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace

from GemAgents.errors import ToolError
from GemAgents.metabolic.contracts import quality_status_contract
from GemAgents.metabolic.library.quality import (
    apply_canonical_chemistry,
    guard_energy_hydrolysis_direction,
)
from GemAgents.metabolic.qc import Auditor, Probe, mass_probe, repair
from GemAgents.metabolic.reconstruction.conditions import growth_tasks
from GemAgents.metabolic.reconstruction.precursor_audit import (
    metabolic_biomass_precursor_audit,
)


def metabolic_quality(
    model,
    biomass: str,
    config: dict,
    *,
    growth_tasks_fn: Callable | None = None,
    reference_growth_fn: Callable | None = None,
    precursor_audit_fn: Callable | None = None,
    repair_fn: Callable | None = None,
) -> tuple[object, dict]:
    """Run declared probes, growth tasks, calibration and static model checks."""
    started = time.perf_counter()
    # Quality evaluation is read-only for the caller.  All normalization,
    # direction guards and repair edits apply to this private working model.
    model = model.copy()
    chemistry_corrections = apply_canonical_chemistry(model)
    # Reference scaffolds and gap-fill candidates are added after the shared
    # universe is loaded.  Reapply the audited energy-direction policy here so
    # those reactions cannot reintroduce reverse ATP synthesis before probes
    # run.  This mutates only the working model that is returned by QC.
    guarded_reactions = []
    for reaction in model.reactions:
        before = tuple(float(value) for value in reaction.bounds)
        if guard_energy_hydrolysis_direction(reaction):
            guarded_reactions.append(
                {
                    "reaction_id": reaction.id,
                    "before_bounds": list(before),
                    "after_bounds": list(reaction.bounds),
                    "reason": reaction.notes.get("qc_direction_guard", "energy_direction_guard"),
                }
            )
    probes = [mass_probe(model)]
    carbon_drains = tuple(
        {metabolite.id: -1.0}
        for metabolite in model.metabolites
        if metabolite.compartment != "e"
        and metabolite.formula
        and "C" in metabolite.elements
    )
    if carbon_drains:
        probes.append(Probe("carbon_free_net_generation", carbon_drains))
    # Each probe is an explicitly defined diagnostic, never added to the
    # exported model.  Only create a probe when its complete carrier pair and
    # hydrolysis cofactors exist; an absent carrier is ``not_applicable`` and
    # must not turn a valid model into a failed QC result.
    carrier_specs = (
        ("ATP", "atp_c", "adp_c", ("h2o_c", "pi_c", "h_c")),
        ("GTP", "gtp_c", "gdp_c", ("h2o_c", "pi_c", "h_c")),
        ("CTP", "ctp_c", "cdp_c", ("h2o_c", "pi_c", "h_c")),
        ("UTP", "utp_c", "udp_c", ("h2o_c", "pi_c", "h_c")),
        ("ITP", "itp_c", "idp_c", ("h2o_c", "pi_c", "h_c")),
        ("NADH", "nadh_c", "nad_c", ("h_c",)),
        ("NADPH", "nadph_c", "nadp_c", ("h_c",)),
        ("FADH2", "fadh2_c", "fad_c", ("h_c",)),
        ("FMNH2", "fmnh2_c", "fmn_c", ("h_c",)),
        ("Q8H2", "q8h2_c", "q8_c", ("h_c",)),
    )
    for name, high, low, cofactors in carrier_specs:
        if all(identifier in model.metabolites for identifier in (high, low, *cofactors)):
            if name in {"ATP", "GTP", "CTP", "UTP", "ITP"}:
                drain = {high: -1, "h2o_c": -1, low: 1, "pi_c": 1, "h_c": 1}
            else:
                drain = {high: -1, low: 1, "h_c": 1}
            probes.append(Probe(name, (drain,)))
    growth_tasks_fn = growth_tasks_fn or growth_tasks
    reference_growth_fn = reference_growth_fn or _default_reference_growth
    precursor_audit_fn = precursor_audit_fn or metabolic_biomass_precursor_audit
    repair_fn = repair_fn or repair
    tasks = growth_tasks_fn(model, biomass, float(config.get("min_growth", 0.01)))
    probe_timeout = int(config.get("qc_timeout", min(int(config.get("solver_timeout", 120)), 30)))
    probe_closed = [biomass]
    probe_closed.extend(
        reaction.id
        for reaction in model.reactions
        if reaction.notes.get("qc_probe_closed")
    )
    auditor = Auditor(tuple(dict.fromkeys(probe_closed)), timeout=probe_timeout)
    repair_search_probes = probes
    if config.get("quality", "audit") == "repair":
        # Search against every declared probe so an accepted repair cannot
        # leave a carrier cycle hidden behind a material-only repair.
        repair_search_probes = probes
        model, report = repair_fn(
            model,
            tuple(repair_search_probes),
            tasks,
            auditor,
            max_edits=int(config.get("max_edits", 10)),
            costs={
                f"{r.id}:{s}": (
                    100.0
                    if r.annotation.get("bigg.reaction") or r.notes.get("reference_reaction")
                    else 10.0
                    if r.genes
                    else 1.0
                )
                for r in model.reactions
                for s in ("+", "-")
            },
        )
        report["repair_search_probes"] = [probe.name for probe in repair_search_probes]
        reported_names = {
            str(row.get("name")) for row in report.get("final_probes", [])
        }
        if reported_names <= set(report["repair_search_probes"]):
            verifier = Auditor(
                auditor.closed, tolerance=auditor.tolerance, cache=False, timeout=auditor.timeout
            )
            report["final_probes"] = [
                asdict(verifier.audit(model, probe)) for probe in probes
            ]
            report["final_verification_lp_calls"] = verifier.lp_calls
    else:
        report = {
            "final_probes": [asdict(auditor.audit(model, p)) for p in probes],
            "final_tasks": [asdict(r) for r in auditor.tasks(model, tasks)],
        }
    reference_growth_result = reference_growth_fn(config)
    if reference_growth_result is not None:
        ceiling, source = reference_growth_result
        with model:
            model.objective = biomass
            before = model.slim_optimize(error_value=None)
        if before is None or not math.isfinite(float(before)):
            raise ToolError("Could not evaluate growth before reference calibration")
        reaction = model.reactions.get_by_id(biomass)
        applied = config.get("quality", "audit") == "repair" and before > ceiling + 1e-7
        if applied:
            if reaction.lower_bound > ceiling:
                raise ToolError("Reference growth ceiling is below the required minimum growth")
            reaction.upper_bound = min(reaction.upper_bound, ceiling)
            reaction.notes["reference_growth_ceiling"] = str(ceiling)
            reaction.notes["reference_growth_medium"] = json.dumps(
                config.get("medium_file") or config.get("medium", "minimal"),
                sort_keys=True,
            )
        calibrated_tasks = tuple(
            replace(task, maximum=ceiling) if task.name == "declared_growth" else task
            for task in tasks
        )
        report["final_tasks"] = [
            asdict(result) for result in auditor.tasks(model, calibrated_tasks)
        ]
        calibrated_declared = next(
            result for result in report["final_tasks"] if result["name"] == "declared_growth"
        )
        report["growth_calibration"] = {
            "method": "same-medium public reference growth ceiling",
            "source": source,
            "reference_growth": ceiling,
            "uncalibrated_growth": float(before),
            "calibrated_growth": calibrated_declared["maximum"],
            "applied": applied,
        }
        reaction.notes["model_role"] = "calibration_derived_model" if applied else "raw_model"
        reaction.notes["reference_growth_claim_limit"] = (
            "calibration_only; not phenotype accuracy or energy-cycle evidence"
        )
    report["biomass_precursors"] = precursor_audit_fn(
        model,
        biomass,
        timeout=probe_timeout,
        tolerance=auditor.tolerance,
    )
    report["repair_search_status"] = report.get("status", "not_requested")
    report["energy_direction_guards"] = guarded_reactions
    report["canonical_chemistry_corrections"] = chemistry_corrections
    report.setdefault("repair_search_probes", [probe.name for probe in repair_search_probes])
    energy_results = {
        row["name"]: row
        for row in report.get("final_probes", [])
        if row.get("name") in {
            "ATP", "GTP", "CTP", "UTP", "ITP", "NADH", "NADPH",
            "FADH2", "FMNH2", "Q8H2",
        }
    }
    report["energy_cycle_qc"] = {
        "boundary_closure": "all_model_boundary_reactions_closed_in_probe_copy",
        "threshold": float(auditor.tolerance),
        "carriers": energy_results,
        "status": (
            "not_applicable"
            if not energy_results
            else "pass"
            if all(row.get("status") == "pass" for row in energy_results.values())
            else "needs_review"
        ),
        "interpretation": (
            "A nonzero maximum or witness indicates an energy-carrier cycle under "
            "closed-boundary audit conditions; it is not evidence of biological energy production."
        ),
    }
    report["status"] = (
        "passed_declared_probes"
        if all(r["status"] == "pass" for r in report["final_probes"] + report["final_tasks"])
        else "needs_review"
    )
    report["quality_profile"] = "legacy_audit_scoped_v2_energy_carbon"
    report["quality_claim"] = "declared checks only; no external biological validation"
    report["externally_validated"] = False
    # Keep direction/bounds checks explicit at the model level as well as in
    # the library build. A model can acquire new reactions during mapping or
    # gap filling, so the library report alone is not sufficient evidence.
    direction_counts = Counter()
    invalid_bounds = []
    for reaction in model.reactions:
        lower, upper = reaction.bounds
        if not (math.isfinite(lower) and math.isfinite(upper)):
            direction_counts["invalid"] += 1
            invalid_bounds.append(
                {"reaction": reaction.id, "bounds": [lower, upper], "reason": "nonfinite"}
            )
        elif lower > upper + 1e-9:
            direction_counts["invalid"] += 1
            invalid_bounds.append(
                {"reaction": reaction.id, "bounds": [lower, upper], "reason": "reversed_order"}
            )
        elif abs(lower) <= 1e-9 and abs(upper) <= 1e-9:
            direction_counts["blocked"] += 1
        elif lower < -1e-9 and upper > 1e-9:
            direction_counts["reversible"] += 1
        elif lower >= -1e-9 and upper > 1e-9:
            direction_counts["forward"] += 1
        elif lower < -1e-9 and upper <= 1e-9:
            direction_counts["reverse"] += 1
        else:
            direction_counts["invalid"] += 1
            invalid_bounds.append(
                {"reaction": reaction.id, "bounds": [lower, upper], "reason": "unclassified"}
            )
    imbalanced, unknown = [], []
    for reaction in model.reactions:
        if (
            reaction.boundary
            or reaction.id == biomass
            or "biomass" in reaction.id.casefold()
        ):
            continue
        if any(
            not metabolite.formula
            or metabolite.charge is None
            or not metabolite.elements
            or "R" in metabolite.elements
            or "X" in metabolite.elements
            for metabolite in reaction.metabolites
        ):
            unknown.append(reaction.id)
            continue
        try:
            balance = reaction.check_mass_balance()
            if balance:
                imbalanced.append({"reaction": reaction.id, "residual": balance})
        except (ValueError, TypeError):
            unknown.append(reaction.id)
    report["static_balance"] = {"imbalanced": imbalanced, "unknown_formula_or_charge": unknown}
    report["direction"] = {
        "counts": dict(sorted(direction_counts.items())),
        "invalid_bounds": invalid_bounds,
    }
    report["coverage_note"] = (
        "Strict closure only; listed cofactors/compartments only; MEMOTE is a separate report"
    )
    report["declared_checks_passed"] = (
        report["status"] == "passed_declared_probes"
        and not imbalanced
        and not unknown
        and not invalid_bounds
        and report["biomass_precursors"]["status"] == "pass"
    )
    report.update(quality_status_contract(report))
    report["algorithm"] = "independent_fast_model_qc"
    report["probe_timeout_seconds"] = probe_timeout
    report["elapsed_seconds"] = time.perf_counter() - started
    return model, report


def _default_reference_growth(config: dict):
    from GemAgents.metabolic.reconstruction.conditions import reference_growth

    return reference_growth(config)
