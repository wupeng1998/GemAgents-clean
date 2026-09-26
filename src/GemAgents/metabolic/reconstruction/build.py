"""Evidence-guided model construction adapters."""

from __future__ import annotations

import copy
import math
import time
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.evidence.mapping import metabolic_evidence_status
from GemAgents.metabolic.io import metabolic_json
from GemAgents.metabolic.media.selection import metabolic_set_medium


def metabolic_build_model(universal, universe_path: Path, mapped: dict, config: dict, out: Path):
    from cobra import Model as CobraModel
    from cobra.io import read_sbml_model
    from cobra.manipulation import remove_genes
    from cobra.util.solver import linear_reaction_coefficients

    if not mapped:
        raise ToolError(
            "No enzyme evidence maps to the reaction library; refusing evidence-free model"
        )
    objectives = list(linear_reaction_coefficients(universal))
    if len(objectives) != 1:
        raise ToolError("Specify a universal model with one biomass objective")
    biomass = objectives[0].id
    medium = metabolic_set_medium(universal, config.get("medium", "minimal"))
    if config.get("engine", "carveme") == "carveme":
        import pandas as pd
        from carveme.reconstruction.carving import carve_model
        from reframed import load_cbmodel, save_cbmodel, set_default_solver
        from reframed.core.transformation import apply_bounds

        set_default_solver("scip")
        bag = load_cbmodel(str(universe_path), flavor="bigg")
        apply_bounds(bag)
        # Preserve explicit media during construction; all other exchange uptake closes.
        hard = {"R_" + r.id: r.bounds for r in universal.exchanges}
        scores = pd.DataFrame(
            [
                {
                    "reaction": "R_" + rid,
                    "normalized_score": row["score"],
                    "GPR": " or ".join(row["gpr_genes"]),
                }
                for rid, row in mapped.items()
            ]
        )
        # Current reframed's SCIP interface has no set_bounds method used by
        # CarveMe's hard-constraint path. Apply identical bounds before solver creation.
        for rid, bounds in hard.items():
            bag.set_flux_bounds(rid, *bounds)
        built = carve_model(bag, scores, inplace=True)
        if built is None:
            raise ToolError(
                "CarveMe carving failed; check reconstruction.log and medium feasibility"
            )
        temporary = out / "engine_model.xml"
        save_cbmodel(built, str(temporary), flavor="bigg")
        model = read_sbml_model(str(temporary))
    else:
        model = CobraModel("ncbi_evidence_draft")
        keep = set(mapped) | {biomass} | {r.id for r in universal.exchanges}
        # Other biomass templates must not become cheap gap-filling pathways.
        for r in universal.reactions:
            if "biomass" in r.id.lower() and r.id != biomass and not r.boundary:
                r.bounds = (0, 0)
        model.add_reactions([universal.reactions.get_by_id(r).copy() for r in sorted(keep)])
        model.objective = biomass
        # Reconstructor-style pFBA: evidence reactions have zero gap-fill cost.
        # The draft is an exact subset of this bag, so deleting and reinserting
        # thousands of identical reactions adds no information. Solve on the bag.
        # Use the declared absolute growth task instead of a fraction of a rich-
        # medium universal model's often unrealistic maximum growth.
        started = time.perf_counter()
        with universal:
            target = universal.reactions.get_by_id(biomass)
            target.lower_bound = max(target.lower_bound, float(config.get("min_growth", 0.01)))
            universal.objective = universal.problem.Objective(0, direction="min")
            universal.objective.set_linear_coefficients(
                {
                    variable: 0.0 if reaction.id in keep else 1.0
                    for reaction in universal.reactions
                    for variable in (reaction.forward_variable, reaction.reverse_variable)
                }
            )
            solution = universal.optimize(raise_error=True)
            if solution.status != "optimal" or any(not math.isfinite(v) for v in solution.fluxes):
                raise ToolError(f"Gap filling did not reach an optimal solution: {solution.status}")
            additions = set(solution.fluxes[solution.fluxes.abs() > 1e-8].index) - keep
            metabolic_json(
                out / "engine-report.json",
                {
                    "algorithm": "Reconstructor-style pFBA on unchanged public reaction bag",
                    "growth_constraint": float(config.get("min_growth", 0.01)),
                    "status": solution.status,
                    "objective": solution.objective_value,
                    "gapfill_reactions": sorted(additions),
                    "elapsed_seconds": time.perf_counter() - started,
                    "original_reconstructor_cli": False,
                },
            )
        model.add_reactions([universal.reactions.get_by_id(r).copy() for r in sorted(additions)])
    model.solver = "glpk"
    model.solver.configuration.timeout = int(config.get("solver_timeout", 120))
    for reaction in model.reactions:
        entry = mapped.get(reaction.id)
        if reaction.id in universal.reactions:
            source = universal.reactions.get_by_id(reaction.id)
            reaction.annotation = copy.deepcopy(source.annotation)
            reaction.notes.update(copy.deepcopy(source.notes))
        reaction.gene_reaction_rule = " or ".join(entry["gpr_genes"]) if entry else ""
        reaction.notes["reconstruction_evidence"] = metabolic_evidence_status(entry)
        if entry:
            reaction.notes["candidate_genes"] = ";".join(entry["genes"])
            reaction.notes["mapping_ambiguous"] = str(entry["ambiguous"])
    remove_genes(model, [g for g in model.genes if not g.reactions])
    for metabolite in model.metabolites:
        if metabolite.id in universal.metabolites:
            metabolite.annotation = copy.deepcopy(
                universal.metabolites.get_by_id(metabolite.id).annotation
            )
    # Reapply precisely the same medium to exchanges retained in the carved model.
    model.medium = {k: v for k, v in medium.items() if k in model.reactions}
    model.objective = biomass
    value = model.slim_optimize(error_value=None)
    if not math.isfinite(value) or value < float(config.get("min_growth", 0.01)):
        raise ToolError("Reconstructed model cannot satisfy the declared minimum growth")
    return model, biomass
