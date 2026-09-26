#!/usr/bin/env python3
"""Compare an independent Agent reconstruction with matching local BiGG models."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from cobra.exceptions import OptimizationError
from cobra.flux_analysis import pfba
from cobra.io import read_sbml_model
from cobra.util.solver import linear_reaction_coefficients

from benchmarks.evaluate import chemically_equivalent, mapped_gene_overlap
from GemAgents.tools import _metabolic_oxygen_exchange_ids, metabolic_set_medium


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-run", type=Path, required=True)
    parser.add_argument("--bigg-directory", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--organism", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--memote-root", type=Path)
    parser.add_argument("--orthology-map", type=Path)
    return parser.parse_args()


def finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def growth_with_status(model) -> tuple[float | None, str]:
    try:
        value = finite(model.slim_optimize(error_value=None))
    except OptimizationError:
        return None, "infeasible"
    return value, "optimal" if value is not None else "nonfinite"


def objective_ids(model) -> list[str]:
    return sorted(reaction.id for reaction in linear_reaction_coefficients(model))


def glucose_exchange_ids(model) -> set[str]:
    result = set()
    for reaction in model.exchanges:
        if len(reaction.metabolites) != 1:
            continue
        metabolite = next(iter(reaction.metabolites))
        identifiers = {metabolite.id.rsplit("_", 1)[0]}
        raw = metabolite.annotation.get("bigg.metabolite", [])
        identifiers.update([raw] if isinstance(raw, str) else raw)
        if "glc__D" in identifiers:
            result.add(reaction.id)
    return result


def model_measurements(path: Path) -> tuple[dict, object]:
    model = read_sbml_model(str(path))
    native_growth, native_status = growth_with_status(model)
    metabolic_set_medium(model, "minimal")
    aerobic_medium = dict(model.medium)
    aerobic_growth, aerobic_status = growth_with_status(model)
    pfba_total_flux = None
    if aerobic_growth is not None and aerobic_growth > 1e-9:
        solution = pfba(model)
        pfba_total_flux = float(solution.fluxes.abs().sum())

    anaerobic_medium = {
        key: value
        for key, value in aerobic_medium.items()
        if key not in _metabolic_oxygen_exchange_ids(model)
    }
    model.medium = anaerobic_medium
    anaerobic_growth, anaerobic_status = growth_with_status(model)

    carbon_free = {
        key: value
        for key, value in aerobic_medium.items()
        if key not in glucose_exchange_ids(model)
    }
    model.medium = carbon_free
    no_glucose_growth, no_glucose_status = growth_with_status(model)
    model.medium = aerobic_medium

    result = {
        "model": path.stem,
        "path": str(path.resolve()),
        "reactions": len(model.reactions),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "gpr_reactions": sum(bool(reaction.gene_reaction_rule) for reaction in model.reactions),
        "boundary_reactions": len(model.boundary),
        "objective_reactions": objective_ids(model),
        "native_growth": native_growth,
        "native_growth_status": native_status,
        "aerobic_glucose_minimal_growth": aerobic_growth,
        "aerobic_glucose_minimal_status": aerobic_status,
        "anaerobic_glucose_minimal_growth": anaerobic_growth,
        "anaerobic_glucose_minimal_status": anaerobic_status,
        "no_glucose_growth": no_glucose_growth,
        "no_glucose_status": no_glucose_status,
        "aerobic_pfba_total_absolute_flux": pfba_total_flux,
    }
    return result, model


def reaction_aliases(reaction) -> set[str]:
    aliases = {reaction.id}
    raw = reaction.annotation.get("bigg.reaction", [])
    aliases.update([raw] if isinstance(raw, str) else raw)
    return {str(item) for item in aliases if item}


def metabolite_aliases(metabolite) -> set[str]:
    aliases = {metabolite.id}
    raw = metabolite.annotation.get("bigg.metabolite", [])
    aliases.update([raw] if isinstance(raw, str) else raw)
    return {str(item) for item in aliases if item}


def covered(reference_items, agent_items, alias_function) -> int:
    agent_aliases = set().union(*(alias_function(item) for item in agent_items))
    return sum(bool(alias_function(item) & agent_aliases) for item in reference_items)


def gene_overlap(agent, reference, orthology: dict[str, str]) -> tuple[int, int]:
    input_gene_ids = {gene.id for gene in agent.genes}
    reference_gene_ids = {gene.id for gene in reference.genes}
    result = mapped_gene_overlap(input_gene_ids, reference_gene_ids, orthology)
    return result["overlap"], result["comparable"]


def chemical_reaction_coverage(reference, agent) -> int:
    return sum(
        any(chemically_equivalent(candidate, reaction) for candidate in agent.reactions)
        for reaction in reference.reactions
    )


def memote_summary(root: Path | None, label: str) -> dict | None:
    if root is None:
        return None
    path = root / label / "summary.json"
    if not path.is_file():
        return None
    summary = json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": summary.get("status"),
        "score_percent": summary.get("score_percent"),
        "sections": {
            row["section"]: row["score"] * 100 for row in summary.get("sections", [])
        },
        "test_errors": len(summary.get("test_errors", [])),
        "report": summary.get("report"),
    }


def write_tsv(path: Path, rows: list[dict]) -> None:
    keys = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: object, digits: int = 6) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> None:
    args = parse_args()
    args.output_directory.mkdir(parents=True, exist_ok=True)
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    orthology = (
        json.loads(args.orthology_map.read_text(encoding="utf-8"))
        if args.orthology_map
        else {}
    )
    registry_by_id = {row["id"]: row for row in registry["records"]}
    xml_paths = sorted(args.bigg_directory.glob("*.xml"))
    target_ids = sorted(
        row["id"] for row in registry["records"] if row.get("organism") == args.organism
    )
    missing_models = [
        model_id
        for model_id in target_ids
        if not (args.bigg_directory / f"{model_id}.xml").is_file()
    ]
    if missing_models:
        raise SystemExit(f"Registry models missing from directory: {missing_models}")

    inventory = []
    eukaryotic_markers = (
        "Homo sapiens",
        "Mus musculus",
        "Cricetulus griseus",
        "Saccharomyces cerevisiae",
        "Plasmodium ",
        "Trypanosoma ",
        "Phaeodactylum ",
    )
    for path in xml_paths:
        record = registry_by_id.get(path.stem, {})
        organism = record.get("organism", "registry entry unavailable")
        if organism == args.organism:
            status = "reconstructed_from_exact_local_genome"
        elif any(marker in organism for marker in eukaryotic_markers):
            status = "not_built_eukaryotic_frontend_unavailable"
        else:
            status = "not_built_matching_genome_unavailable"
        inventory.append(
            {
                "model": path.stem,
                "organism": organism,
                "reference_path": str(path.resolve()),
                "reconstruction_status": status,
            }
        )
    write_tsv(args.output_directory / "bigg-inventory.tsv", inventory)

    agent_row, agent = model_measurements(args.agent_run / "model.xml")
    agent_row["model"] = "Agent_MG1655"
    agent_row["memote"] = memote_summary(args.memote_root, "Agent_MG1655")
    rows = [agent_row]
    references = []
    for model_id in target_ids:
        row, model = model_measurements(args.bigg_directory / f"{model_id}.xml")
        reaction_count = chemical_reaction_coverage(model, agent)
        metabolite_count = covered(model.metabolites, agent.metabolites, metabolite_aliases)
        shared_genes, comparable_genes = gene_overlap(agent, model, orthology)
        row.update(
            {
                "agent_reaction_coverage_count": reaction_count,
                "agent_reaction_coverage_percent": reaction_count / len(model.reactions) * 100,
                "agent_metabolite_coverage_count": metabolite_count,
                "agent_metabolite_coverage_percent": (
                    metabolite_count / len(model.metabolites) * 100
                ),
                "agent_gene_overlap_count": shared_genes,
                "comparable_reference_genes": comparable_genes,
                "agent_gene_overlap_percent": (
                    shared_genes / comparable_genes * 100 if comparable_genes else None
                ),
                "memote": memote_summary(args.memote_root, model_id),
            }
        )
        rows.append(row)
        references.append(model)

    scalar_rows = []
    for row in rows:
        scalar_rows.append(
            {
                key: (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                )
                for key, value in row.items()
            }
        )
    all_keys = []
    for row in scalar_rows:
        all_keys.extend(key for key in row if key not in all_keys)
    for row in scalar_rows:
        for key in all_keys:
            row.setdefault(key, None)
    write_tsv(args.output_directory / "comparison.tsv", scalar_rows)

    payload = {
        "scope": {
            "bigg_xml_files": len(xml_paths),
            "registry_records": len(registry["records"]),
            "exact_local_genome_organism": args.organism,
            "matching_reference_models": target_ids,
            "matching_reference_model_count": len(target_ids),
            "nonmatching_models_not_used_as_reconstruction_input": len(xml_paths) - len(target_ids),
        },
        "models": rows,
    }
    (args.output_directory / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest = json.loads((args.agent_run / "manifest.json").read_text(encoding="utf-8"))
    quality = json.loads((args.agent_run / "quality.json").read_text(encoding="utf-8"))
    gapfill = json.loads((args.agent_run / "gapfill-report.json").read_text(encoding="utf-8"))
    lines = [
        "# Agent 从头重建与本地 BiGG MG1655 模型对比",
        "",
        "## 范围与独立性",
        "",
        f"- BiGG 目录含 {len(xml_paths)} 个 XML；registry 含 {len(registry['records'])} 个条目。",
        (
            f"- 本地基因组能严格对应 {len(target_ids)} 个 `{args.organism}` 模型："
            f"`{'`, `'.join(target_ids)}`。"
        ),
        (
            f"- 其余 {len(xml_paths) - len(target_ids)} 个 XML 未用作构建输入；"
            "状态见 `bigg-inventory.tsv`。"
        ),
        (
            "- Agent 输入为 NC_000913.3 基因组、NCBI 注释及独立 CLEAN 预测；"
            "发布 SBML 只用于构建后的比较。"
        ),
        f"- 禁止项目数据使用标志：`{manifest.get('prohibited_projects_used')}`。",
        "",
        "## 基本信息与统一模拟",
        "",
        "所有模型均在相同的 Agent 定义葡萄糖最小培养基下模拟；厌氧条件仅移除分子氧摄取。",
        "",
        (
            "| 模型 | 反应 | 代谢物 | 基因 | GPR反应 | Boundary | 有氧生长 | "
            "厌氧生长 | 无葡萄糖生长 | pFBA总绝对通量 | MEMOTE |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        score = row.get("memote", {}).get("score_percent") if row.get("memote") else None
        lines.append(
            "| "
            + " | ".join(
                [
                    row["model"],
                    str(row["reactions"]),
                    str(row["metabolites"]),
                    str(row["genes"]),
                    str(row["gpr_reactions"]),
                    str(row["boundary_reactions"]),
                    fmt(row["aerobic_glucose_minimal_growth"]),
                    fmt(row["anaerobic_glucose_minimal_growth"]),
                    fmt(row["no_glucose_growth"]),
                    fmt(row["aerobic_pfba_total_absolute_flux"], 2),
                    fmt(score, 2),
                ]
            )
            + " |"
        )
    reference_rows = rows[1:]
    aerobic_values = [row["aerobic_glucose_minimal_growth"] for row in reference_rows]
    anaerobic_values = [row["anaerobic_glucose_minimal_growth"] for row in reference_rows]
    pfba_values = [row["aerobic_pfba_total_absolute_flux"] for row in reference_rows]
    calibration = quality.get("growth_calibration", {})
    final_probes = quality.get("final_probes", [])
    lines.extend(
        [
            "",
            "## 主要模拟差异",
            "",
            (
                f"- 发布模型有氧范围为 {min(aerobic_values):.6f}–"
                f"{max(aerobic_values):.6f}；Agent 为 "
                f"{agent_row['aerobic_glucose_minimal_growth']:.6f}。"
            ),
            (
                f"- 发布模型厌氧范围为 {min(anaerobic_values):.6f}–"
                f"{max(anaerobic_values):.6f}；Agent 为 "
                f"{agent_row['anaerobic_glucose_minimal_growth']:.6f}。"
            ),
            (
                f"- 发布模型有氧 pFBA 总绝对通量范围为 {min(pfba_values):.2f}–"
                f"{max(pfba_values):.2f}；Agent 为 "
                f"{agent_row['aerobic_pfba_total_absolute_flux']:.2f}。"
            ),
            (
                f"- 生长校准：`{calibration.get('method')}`；校准前 "
                f"{fmt(calibration.get('uncalibrated_growth'))}，校准后 "
                f"{fmt(calibration.get('reference_growth'))}。"
            ),
            (
                "- 最终闭边界探针："
                + ", ".join(
                    f"{probe.get('name')}={fmt(probe.get('maximum'))}"
                    for probe in final_probes
                )
                + "。"
            ),
            (
                "- `NA` 的无葡萄糖结果表示 LP 因维持反应下限不可行，而不是观察到生长；"
                "Agent 的数值零同样表示不能利用该培养基生长。"
            ),
            "",
            "## Agent 对发布模型的覆盖",
            "",
            "| 发布模型 | 化学等价反应覆盖 | 代谢物覆盖 | 显式 orthology 基因覆盖 |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in rows[1:]:
        lines.append(
            f"| {row['model']} | {row['agent_reaction_coverage_count']}/{row['reactions']} "
            f"({row['agent_reaction_coverage_percent']:.2f}%) | "
            f"{row['agent_metabolite_coverage_count']}/{row['metabolites']} "
            f"({row['agent_metabolite_coverage_percent']:.2f}%) | "
            f"{row['agent_gene_overlap_count']}/{row['comparable_reference_genes']} "
            f"({fmt(row['agent_gene_overlap_percent'], 2)}%) |"
        )
    if any(row.get("memote") for row in rows):
        lines.extend(
            [
                "",
                "## MEMOTE 0.17.0",
                "",
                (
                    "| 模型 | 总分 | Consistency | Metabolite annotation | "
                    "Reaction annotation | Gene annotation | SBO annotation | 非计分错误 |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        section_keys = [
            "consistency",
            "annotation_met",
            "annotation_rxn",
            "annotation_gene",
            "annotation_sbo",
        ]
        for row in rows:
            memote = row.get("memote")
            if not memote:
                continue
            sections = memote["sections"]
            values = [fmt(sections.get(key), 2) for key in section_keys]
            lines.append(
                f"| {row['model']} | {fmt(memote.get('score_percent'), 2)} | "
                + " | ".join(values)
                + f" | {memote.get('test_errors', 0)} |"
            )
        lines.extend(
            [
                "",
                (
                    "MEMOTE 总分按公开默认权重计算；非计分测试错误单列，"
                    "没有将其悄悄折算为 0 分。"
                ),
            ]
        )
    scenarios = gapfill.get("growth_scenarios", [])
    lines.extend(
        [
            "",
            "## 构建与 QC 摘要",
            "",
            f"- 最终状态：`{manifest.get('status')}`；QC：`{quality.get('status')}`。",
            (
                f"- NCBI EC 蛋白：{manifest.get('ncbi_proteins_with_ec')}；"
                "CLEAN 入模基因："
                f"{manifest.get('clean_prediction', {}).get('model_genes')}。"
            ),
            (
                f"- Gapfill 阶段：`{gapfill.get('stage')}`；"
                f"新增反应：{len(gapfill.get('additions', []))}；chemistry fallback："
                f"{len(gapfill.get('template_gapfill_additions', []))}。"
            ),
            f"- 要求并验证的生长条件：{', '.join(row.get('name', '') for row in scenarios)}。",
            f"- QC 声明探针全部通过：`{quality.get('declared_checks_passed')}`。",
            "",
            "## 解释边界",
            "",
            (
                "同一菌株的六个发布模型代表不同年代和网络范围，因此不应期待反应数或"
                "生长值完全一致。覆盖率是 ID/注释别名层面的网络覆盖，不等同于实验准确率。"
                "未匹配模型需要各自的基因组与原核/真核适配前端后才能进行无泄漏重建。"
            ),
            "",
        ]
    )
    (args.output_directory / "comparison.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
