"""Audit the v6 iML1515 reconstruction for closed-boundary material leaks.

This is a reproducible diagnostic for the user's glucose-growth discrepancy. It
rebuilds the model from the persisted initial model and v6 gap-fill additions,
then uses the repository CER Auditor plus individual metabolite demand LPs.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from pathlib import Path

from cobra import Reaction
from cobra.io import read_sbml_model

from GemAgents.metabolic.library.quality import (
    guard_energy_hydrolysis_direction,
    load_reaction_quality_table,
)
from GemAgents.metabolic.qc import Auditor, mass_probe
from GemAgents.metabolic.reconstruction.fallbacks import disposal_only_bounds

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(
    os.environ.get("GEMAGENTS_DIAGNOSTIC_RUN", str(ROOT / "runs/iml1515_gemagents_20260922_v6"))
)
LIB = ROOT / "data/reaction_library_public_union_annotated_20260922"
OUT = RUN / "cycle-audit.json"


def _guarded_bounds(reaction):
    bounds = reaction.bounds
    stoichiometry = {m.id: coefficient for m, coefficient in reaction.metabolites.items()}
    for carrier, low in (("atp_c", "adp_c"), ("gtp_c", "gdp_c")):
        hydrolysis = {
            carrier: -1.0,
            "h2o_c": -1.0,
            low: 1.0,
            "pi_c": 1.0,
            "h_c": 1.0,
        }
        if stoichiometry == hydrolysis:
            return max(0.0, bounds[0]), bounds[1]
        if stoichiometry == {m: -coefficient for m, coefficient in hydrolysis.items()}:
            return bounds[0], min(0.0, bounds[1])
    return bounds


def _additions():
    report = json.loads((RUN / "gapfill-report.json").read_text())
    return report, set(report["growth_scenarios"][0]["additions"])


def _rebuild():
    report, additions = _additions()
    model = read_sbml_model(str(RUN / "initial_model.xml"))
    # Decompress to a temporary path because the libSBML binding accepts a
    # filename here (not a binary file object) in cobra 0.32.
    with (
        gzip.open(LIB / "universe_full.xml.gz", "rb") as source,
        tempfile.NamedTemporaryFile(suffix=".xml") as target,
    ):
        target.write(source.read())
        target.flush()
        universe = read_sbml_model(target.name)
    quality = load_reaction_quality_table(LIB / "reaction_quality.tsv")
    for rid in additions:
        source = universe.reactions.get_by_id(rid)
        model.add_reactions([source.copy()])
        row = quality.get(rid, {})
        if source.boundary:
            model.reactions.get_by_id(rid).bounds = disposal_only_bounds(source)
        elif row.get("status") == "unknown_formula_or_charge":
            model.reactions.get_by_id(rid).bounds = _guarded_bounds(source)
    model.objective = "biomass"
    medium = report["medium"]
    model.medium = {rid: value for rid, value in medium.items() if rid in model.reactions}
    return model, universe, quality, report, additions


def _close_all_boundaries(model):
    """Close every boundary supply while preserving product disposal."""
    for reaction in model.boundary:
        if len(reaction.metabolites) != 1:
            reaction.bounds = (0.0, 0.0)
            continue
        coefficient = next(iter(reaction.metabolites.values()))
        if coefficient < 0:
            reaction.bounds = (max(0.0, reaction.lower_bound), max(0.0, reaction.upper_bound))
        else:
            reaction.bounds = (min(0.0, reaction.lower_bound), min(0.0, reaction.upper_bound))


def _demand_audit(model, ids):
    rows = []
    work = model.copy()
    _close_all_boundaries(work)
    for index, metabolite_id in enumerate(ids):
        if metabolite_id not in work.metabolites:
            continue
        with work:
            demand = Reaction(f"__cycle_demand_{index}", lower_bound=0, upper_bound=1000)
            demand.add_metabolites({work.metabolites.get_by_id(metabolite_id): -1.0})
            work.add_reactions([demand])
            work.objective = demand
            solution = work.optimize()
            value = float(solution.objective_value or 0.0) if solution.status == "optimal" else 0.0
            if value <= 1e-7:
                continue
            flux = {
                rid: float(v)
                for rid, v in solution.fluxes.items()
                if rid != demand.id and abs(float(v)) > 1e-7
            }
            rows.append({"metabolite": metabolite_id, "maximum": value, "flux": flux})
    return rows


def _energy_cycle_audit(model):
    """Maximize reverse hydrolysis under strict closed-boundary conditions."""
    work = model.copy()
    _close_all_boundaries(work)
    rows = []
    for rid in ("ATPM",):
        if rid not in work.reactions:
            continue
        reaction = work.reactions.get_by_id(rid)
        work.objective = work.problem.Objective(-reaction.flux_expression, direction="max")
        solution = work.optimize()
        value = float(solution.objective_value or 0.0) if solution.status == "optimal" else 0.0
        rows.append({"reaction": rid, "max_reverse_flux": value, "status": solution.status})
    return rows


def main():
    model, universe, quality, report, additions = _rebuild()
    corrected = model.copy()
    corrected_guards = sum(
        guard_energy_hydrolysis_direction(reaction) for reaction in corrected.reactions
    )
    solution = model.optimize()
    active_flux = {
        rid: float(value) for rid, value in solution.fluxes.items() if abs(float(value)) > 1e-7
    }
    reference = read_sbml_model(str(ROOT / "bigg_model_public/iML1515.xml"))
    reference_growth = float(reference.slim_optimize(error_value=0.0) or 0.0)
    reference_ids = {reaction.id for reaction in reference.reactions}
    closed = tuple(r.id for r in model.boundary)
    cer = Auditor(closed=closed, timeout=60).audit(model, mass_probe(model))
    carriers = [
        "atp_c",
        "adp_c",
        "pi_c",
        "h_c",
        "nad_c",
        "nadh_c",
        "nadp_c",
        "nadph_c",
        "q8_c",
        "q8h2_c",
        "ppi_c",
        "co2_c",
        "nh4_c",
        "o2_c",
        "glc__D_c",
    ]
    demand = _demand_audit(model, carriers)
    all_material_generation = _demand_audit(model, [m.id for m in model.metabolites])
    rescue_ids = {"MPTG", "MCTP1App", "PAPPT3", "UDCPDP", "UDCPDPS", "RBFSa", "DB4PS", "GART"}
    rescue_ablation = {}
    for rid in sorted(rescue_ids):
        with model:
            if rid in model.reactions:
                model.reactions.get_by_id(rid).bounds = (0.0, 0.0)
            rescue_ablation[rid] = float(model.slim_optimize(error_value=0.0) or 0.0)
    with model:
        for rid in rescue_ids:
            if rid in model.reactions:
                model.reactions.get_by_id(rid).bounds = (0.0, 0.0)
        rescue_ablation["all_rescue_closed"] = float(model.slim_optimize(error_value=0.0) or 0.0)
    balances = []
    for reaction in model.reactions:
        if reaction.boundary:
            continue
        residual = reaction.check_mass_balance()
        if residual:
            balances.append(
                {
                    "reaction": reaction.id,
                    "residual": residual,
                    "status": quality.get(reaction.id, {}).get("status"),
                }
            )
    result = {
        "model": {
            "reactions": len(model.reactions),
            "metabolites": len(model.metabolites),
            "growth": float(model.slim_optimize()),
        },
        "qc_direction_correction": {
            "guards_applied": corrected_guards,
            "growth_after_atpm_guard": float(corrected.slim_optimize(error_value=0.0) or 0.0),
            "atpm_bounds_after_guard": list(corrected.reactions.ATPM.bounds)
            if "ATPM" in corrected.reactions
            else None,
        },
        "reference_report_growth": report["growth"],
        "published_iml1515_default_medium_growth": reference_growth,
        "active_flux_top": dict(sorted(active_flux.items(), key=lambda pair: -abs(pair[1]))[:100]),
        "active_flux_reactions_absent_from_published": {
            rid: value
            for rid, value in active_flux.items()
            if rid not in reference_ids and abs(value) > 1e-2
        },
        "guarded_energy_shortcut_flux": {
            rid: active_flux.get(rid, 0.0) for rid in ("ATPM", "PPK", "PPA", "PTPATi")
        },
        "additions": sorted(additions),
        "addition_status": {
            status: sorted(rid for rid in additions if quality.get(rid, {}).get("status") == status)
            for status in {quality.get(rid, {}).get("status") for rid in additions}
        },
        "cer_joint_material": {
            "status": cer.status,
            "maximum": cer.maximum,
            "detail": cer.detail,
            "witness": cer.witness,
        },
        "carrier_net_generation": demand,
        "energy_reverse_cycle": _energy_cycle_audit(model),
        "all_material_net_generation": all_material_generation,
        "rescue_ablation_growth": rescue_ablation,
        "unbalanced_internal_reactions": balances,
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "model",
                    "reference_report_growth",
                    "addition_status",
                    "cer_joint_material",
                    "carrier_net_generation",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
