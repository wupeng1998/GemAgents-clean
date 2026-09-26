#!/usr/bin/env python3
"""Compare a GemAgents reconstruction with the published iML1515 model."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

from cobra.exceptions import OptimizationError
from cobra.io import model_from_dict, read_sbml_model
from cobra.util.solver import linear_reaction_coefficients

from GemAgents.tools import _metabolic_oxygen_exchange_ids, metabolic_set_medium

REFERENCE_PREFIX = "REF_ecoli_mg1655_"


def load_reference(path: Path):
    if path.name.lower().endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return model_from_dict(json.load(handle))
    return read_sbml_model(str(path))


def finite_growth(model) -> float | None:
    try:
        value = model.slim_optimize(error_value=None)
    except OptimizationError:
        return None
    return float(value) if value is not None and math.isfinite(float(value)) else None


def format_growth(value: float | None) -> str:
    return "NA (infeasible)" if value is None else f"{value:.6g}"


def glucose_exchange_ids(model) -> set[str]:
    result = set()
    for reaction in model.exchanges:
        if len(reaction.metabolites) != 1:
            continue
        metabolite = next(iter(reaction.metabolites))
        aliases = {metabolite.id.rsplit("_", 1)[0]}
        raw = metabolite.annotation.get("bigg.metabolite", [])
        aliases.update([raw] if isinstance(raw, str) else raw)
        if "glc__D" in aliases:
            result.add(reaction.id)
    return result


def measurements(model) -> dict:
    native = finite_growth(model)
    metabolic_set_medium(model, "minimal")
    aerobic_medium = dict(model.medium)
    aerobic = finite_growth(model)
    model.medium = {
        key: value
        for key, value in aerobic_medium.items()
        if key not in _metabolic_oxygen_exchange_ids(model)
    }
    anaerobic = finite_growth(model)
    model.medium = {
        key: value
        for key, value in aerobic_medium.items()
        if key not in glucose_exchange_ids(model)
    }
    no_glucose = finite_growth(model)
    model.medium = aerobic_medium
    return {
        "reactions": len(model.reactions),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "gpr_reactions": sum(bool(r.gene_reaction_rule) for r in model.reactions),
        "boundary_reactions": len(model.boundary),
        "objective_reactions": sorted(r.id for r in linear_reaction_coefficients(model)),
        "native_growth": native,
        "aerobic_minimal_growth": aerobic,
        "anaerobic_minimal_growth": anaerobic,
        "no_glucose_growth": no_glucose,
    }


def aliases(item, annotation_key: str) -> set[str]:
    item_id = item.id.removeprefix(REFERENCE_PREFIX)
    values = {item_id}
    raw = item.annotation.get(annotation_key, [])
    values.update([raw] if isinstance(raw, str) else raw)
    return {str(value) for value in values if value}


def reference_coverage(reference_items, agent_items, annotation_key: str) -> tuple[int, int]:
    agent_aliases = set().union(*(aliases(item, annotation_key) for item in agent_items))
    covered = sum(bool(aliases(item, annotation_key) & agent_aliases) for item in reference_items)
    return covered, len(reference_items)


def biomass_comparison(agent, reference) -> dict:
    agent_id = next(iter(linear_reaction_coefficients(agent))).id
    reference_id = next(iter(linear_reaction_coefficients(reference))).id
    agent_reaction = agent.reactions.get_by_id(agent_id)
    reference_reaction = reference.reactions.get_by_id(reference_id)
    agent_coefficients = {
        met.id.removeprefix(REFERENCE_PREFIX): float(value)
        for met, value in agent_reaction.metabolites.items()
    }
    reference_coefficients = {
        met.id: float(value) for met, value in reference_reaction.metabolites.items()
    }
    shared = sorted(set(agent_coefficients) & set(reference_coefficients))
    exact = sum(
        math.isclose(agent_coefficients[key], reference_coefficients[key], abs_tol=1e-12)
        for key in shared
    )
    dot = sum(agent_coefficients[key] * reference_coefficients[key] for key in shared)
    agent_norm = math.sqrt(sum(value * value for value in agent_coefficients.values()))
    reference_norm = math.sqrt(sum(value * value for value in reference_coefficients.values()))
    return {
        "agent_reaction": agent_id,
        "reference_reaction": reference_id,
        "agent_metabolites": len(agent_coefficients),
        "reference_metabolites": len(reference_coefficients),
        "shared_metabolites": len(shared),
        "shared_coefficients_exact": exact,
        "coefficient_cosine_similarity": dot / (agent_norm * reference_norm),
    }


def percent(count: int, total: int) -> float:
    return 100.0 * count / total if total else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-run", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    agent = read_sbml_model(str(args.agent_run / "model.xml"))
    reference = load_reference(args.reference)
    agent_metrics = measurements(agent)
    reference_metrics = measurements(reference)
    reaction_count, reaction_total = reference_coverage(
        reference.reactions, agent.reactions, "bigg.reaction"
    )
    metabolite_count, metabolite_total = reference_coverage(
        reference.metabolites, agent.metabolites, "bigg.metabolite"
    )
    agent_gene_ids = {gene.id for gene in agent.genes}
    reference_gene_ids = {gene.id for gene in reference.genes}
    shared_genes = agent_gene_ids & reference_gene_ids
    agent_gene_symbols = {
        str(gene.annotation["gene_symbol"]).lower()
        for gene in agent.genes
        if gene.annotation.get("gene_symbol")
    }
    reference_gene_symbols = {str(gene.name).lower() for gene in reference.genes if gene.name}
    shared_gene_symbols = agent_gene_symbols & reference_gene_symbols

    manifest = json.loads((args.agent_run / "manifest.json").read_text(encoding="utf-8"))
    biomass = json.loads((args.agent_run / "biomass-selection.json").read_text(encoding="utf-8"))
    gapfill = json.loads((args.agent_run / "gapfill-report.json").read_text(encoding="utf-8"))
    quality = json.loads((args.agent_run / "quality.json").read_text(encoding="utf-8"))
    additions = {
        reaction_id
        for scenario in gapfill.get("growth_scenarios", [])
        for reaction_id in scenario.get("additions", [])
    }
    payload = {
        "comparison_scope": {
            "track": manifest.get("evaluation_track"),
            "reference_used_as_gapfill_candidate_source": True,
            "reference_is_independent_validation": False,
            "agent_model": str((args.agent_run / "model.xml").resolve()),
            "published_reference": str(args.reference.resolve()),
        },
        "biomass_selection": {
            "selected_template": biomass.get("selected_template"),
            "selection_reason": biomass.get("selection_reason"),
            "ranked_templates": biomass.get("ranked_templates"),
        },
        "models": {"GemAgents": agent_metrics, "published_iML1515": reference_metrics},
        "coverage_of_published_model": {
            "reactions": reaction_count,
            "reaction_total": reaction_total,
            "reaction_percent": percent(reaction_count, reaction_total),
            "metabolites": metabolite_count,
            "metabolite_total": metabolite_total,
            "metabolite_percent": percent(metabolite_count, metabolite_total),
            "genes": len(shared_genes),
            "gene_total": len(reference_gene_ids),
            "gene_percent": percent(len(shared_genes), len(reference_gene_ids)),
            "gene_symbols": len(shared_gene_symbols),
            "agent_gene_symbols": len(agent_gene_symbols),
            "gene_symbol_agent_percent": percent(len(shared_gene_symbols), len(agent_gene_symbols)),
            "reference_gene_symbols": len(reference_gene_symbols),
            "gene_symbol_reference_percent": percent(
                len(shared_gene_symbols), len(reference_gene_symbols)
            ),
        },
        "biomass_equation": biomass_comparison(agent, reference),
        "gapfill": {
            "stage": gapfill.get("stage"),
            "unique_additions": len(additions),
            "reference_prefixed_additions": sum(
                item.startswith(REFERENCE_PREFIX) for item in additions
            ),
            "unverified_gapfill_pool": gapfill.get("unverified_gapfill_pool"),
        },
        "quality": {
            "status": manifest.get("quality_status"),
            "declared_checks_passed": manifest.get("declared_checks_passed"),
            "final_audit_status": manifest.get("final_audit_status"),
            "probe_status": {row["name"]: row["status"] for row in quality.get("final_probes", [])},
            "static_imbalanced_reactions": len(
                quality.get("static_balance", {}).get("imbalanced", [])
            ),
            "unknown_formula_or_charge": len(
                quality.get("static_balance", {}).get("unknown_formula_or_charge", [])
            ),
        },
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    a = agent_metrics
    r = reference_metrics
    c = payload["coverage_of_published_model"]
    b = payload["biomass_equation"]
    q = payload["quality"]
    lines = [
        "# GemAgents E. coli MG1655 与发表 iML1515 对比",
        "",
        "## 对比边界",
        "",
        "本次成功模型属于 reference_assisted 轨道；发表 iML1515 参与了 gap-fill 候选生成，",
        "因此结构覆盖率用于描述结果相似度，不作为独立外部验证指标。",
        "",
        "## 核心结果",
        "",
        "| 指标 | GemAgents | 发表 iML1515 |",
        "|---|---:|---:|",
        f"| 反应 | {a['reactions']} | {r['reactions']} |",
        f"| 代谢物 | {a['metabolites']} | {r['metabolites']} |",
        f"| 基因 | {a['genes']} | {r['genes']} |",
        f"| GPR 反应 | {a['gpr_reactions']} | {r['gpr_reactions']} |",
        (
            f"| 有氧最小培养基生长 | {format_growth(a['aerobic_minimal_growth'])} | "
            f"{format_growth(r['aerobic_minimal_growth'])} |"
        ),
        (
            f"| 厌氧最小培养基生长 | {format_growth(a['anaerobic_minimal_growth'])} | "
            f"{format_growth(r['anaerobic_minimal_growth'])} |"
        ),
        (
            f"| 无葡萄糖生长 | {format_growth(a['no_glucose_growth'])} | "
            f"{format_growth(r['no_glucose_growth'])} |"
        ),
        "",
        "## 覆盖与 biomass",
        "",
        f"- 发表反应覆盖：{c['reactions']}/{c['reaction_total']} ({c['reaction_percent']:.2f}%)。",
        (
            f"- 发表代谢物覆盖：{c['metabolites']}/{c['metabolite_total']} "
            f"({c['metabolite_percent']:.2f}%)。"
        ),
        (
            f"- 基因直接 ID 重叠：{c['genes']}/{c['gene_total']}；两模型分别使用内部 "
            "`gNNNNNN` 与 b-number，不能据此判断同源性。"
        ),
        (
            f"- 可比较基因符号重叠：{c['gene_symbols']}/{c['agent_gene_symbols']} 个 "
            f"GemAgents 符号 ({c['gene_symbol_agent_percent']:.2f}%)，占发表模型符号 "
            f"{c['gene_symbol_reference_percent']:.2f}%。"
        ),
        (
            f"- biomass 共享代谢物：{b['shared_metabolites']}；系数完全一致："
            f"{b['shared_coefficients_exact']}；余弦相似度："
            f"{b['coefficient_cosine_similarity']:.8f}。"
        ),
        (
            f"- gap-fill 唯一新增反应：{payload['gapfill']['unique_additions']}；"
            f"其中参考前缀反应：{payload['gapfill']['reference_prefixed_additions']}。"
        ),
        "",
        "## 质量结论",
        "",
        f"- 状态：`{q['status']}`；最终审计：`{q['final_audit_status']}`。",
        f"- 探针：`{json.dumps(q['probe_status'], ensure_ascii=False)}`。",
        (
            f"- 静态不平衡反应：{q['static_imbalanced_reactions']}；"
            f"化学式/电荷未知：{q['unknown_formula_or_charge']}。"
        ),
    ]
    (args.output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
