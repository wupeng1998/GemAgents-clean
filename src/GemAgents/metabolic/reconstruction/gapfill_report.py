"""Native gap-fill result annotations and report persistence."""

from __future__ import annotations

import json
from pathlib import Path


def finalize_gapfill_report(
    *,
    model,
    additions: list[str],
    verified_scenarios: list[dict],
    growth: float,
    pruned_additions: list[str],
    unverified_gapfill_ids: set[str],
    boundary_gapfill_ids: set[str],
    template_gapfill_ids: set[str],
    reference_candidate_meta: dict,
    mapped: dict,
    quality: dict,
    biomass_report: dict,
    template: dict,
    rule_details: dict,
    gapfill_stage: str,
    solve_events: list[dict],
    candidate_ids: list[str],
    reference_candidate_ids: set[str],
    selected_medium: dict[str, float],
    forbidden_directions: set[tuple[str, str]],
    out: Path,
    evidence_status,
    write_json,
) -> None:
    """Annotate final reaction evidence and write native gap-fill reports."""
    model.notes["growth_scenarios"] = json.dumps(
        [
            {
                "name": scenario["name"],
                "minimum": scenario["minimum"],
                "medium": scenario["medium"],
                "source": scenario["source"],
                "evidence_role": scenario.get("evidence_role", "declared_input"),
                "reference_growth": scenario.get("reference_growth"),
            }
            for scenario in verified_scenarios
        ],
        sort_keys=True,
    )
    for reaction in model.reactions:
        if reaction.id in unverified_gapfill_ids:
            reaction.gene_reaction_rule = ""
            reaction.notes["evidence_status"] = "public_bigg_gapfill; formula_or_charge_incomplete"
        elif reaction.id in mapped:
            entry = mapped[reaction.id]
            reaction.gene_reaction_rule = entry.get(
                "gpr_rule", " or ".join(sorted(set(entry.get("gpr_genes", []))))
            )
            reaction.notes["evidence_status"] = evidence_status(entry)
        elif reaction.id in reference_candidate_meta:
            metadata = reference_candidate_meta[reaction.id]
            reaction.gene_reaction_rule = metadata.get("gpr", "")
            reaction.notes["evidence_status"] = (
                "public_reference_gpr" if metadata.get("gpr") else "public_reference_no_gpr"
            )
        elif reaction.id in template_gapfill_ids:
            reaction.gene_reaction_rule = ""
            reaction.notes["evidence_status"] = "template_gapfill"
        elif reaction.id in boundary_gapfill_ids:
            reaction.gene_reaction_rule = ""
            reaction.notes["evidence_status"] = "gapfill_boundary"
        reaction.notes["gapfill"] = str(reaction.id in additions).lower()
    write_json(
        out / "biomass-selection.json",
        {**biomass_report, "template": template, "gpr_resolution": rule_details},
    )
    write_json(
        out / "gapfill-report.json",
        {
            "status": "completed",
            "solve_events": solve_events,
            "algorithm": "v6 weighted LP gap-fill",
            "stage": gapfill_stage,
            "candidate_pool": len(candidate_ids),
            "reference_candidate_pool": len(reference_candidate_ids),
            "template_gapfill_pool": len(template_gapfill_ids),
            "unverified_gapfill_pool": len(unverified_gapfill_ids),
            "boundary_gapfill_pool": len(boundary_gapfill_ids),
            "additions": additions,
            "growth": float(growth),
            "medium": selected_medium,
            "growth_scenarios": verified_scenarios,
            "pruned_redundant_additions": pruned_additions,
            "strict_qc_only": not any(
                quality.get(rid, {}).get("status") != "pass" for rid in additions
            ),
            "quarantined_additions": [
                rid for rid in additions if quality.get(rid, {}).get("status") != "pass"
            ],
            "template_gapfill_additions": [rid for rid in additions if rid in template_gapfill_ids],
            "unverified_gapfill_additions": [
                rid for rid in additions if rid in unverified_gapfill_ids
            ],
            "boundary_gapfill_additions": [rid for rid in additions if rid in boundary_gapfill_ids],
            "forbidden_directions": [list(item) for item in sorted(forbidden_directions)],
        },
    )
