"""Reproducible CLEAN input preparation for the reconstruction pipeline."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from GemAgents.contracts import DEFAULT_CLEAN_TOP_FRACTION
from GemAgents.errors import ToolError
from GemAgents.metabolic.predictions import clean_prediction_input_ids
from GemAgents.metabolic.reconstruction.clean_runtime import run_clean_predictions


def prepare_clean_inputs(
    *,
    config: dict,
    out: Path,
    proteins: list[tuple[str, str]],
    evidence: list[dict],
    manifest: dict,
    phase,
    write_fasta,
    import_clean_predictions,
    write_json,
) -> tuple[set[str], dict[str, str], list[dict], set[str]]:
    """Write CLEAN candidates, normalize gene IDs and persist annotation evidence."""
    # CLEAN must only see proteins without an NCBI EC assignment. Always emit
    # the exact candidate FASTA/TSV, even when predictions are not supplied,
    # so an external CLEAN run is reproducible.
    ncbi_ec_ids = {str(row.get("gene_id")) for row in evidence if row.get("ec")}
    clean_candidates = [
        (protein_id, sequence)
        for protein_id, sequence in proteins
        if protein_id not in ncbi_ec_ids
    ]
    phase("clean_input")
    clean_fasta = out / "clean-input-no-ncbi.faa"
    clean_tsv = out / "clean-input-no-ncbi.tsv"
    write_fasta(clean_candidates, clean_fasta)
    with clean_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["protein_id", "sequence", "ec_number", "confidence"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            {
                "protein_id": protein_id,
                "sequence": sequence,
                "ec_number": "",
                "confidence": "",
            }
            for protein_id, sequence in clean_candidates
        )
    clean_rows: list[dict] = []
    manifest["clean_prediction"] = {
        "enabled": bool(config.get("clean_predictions") or config.get("clean_runtime")),
        # ``pending`` is deliberately distinct from an empty prediction set:
        # an external table can exist while belonging to a different genome.
        "status": (
            "pending"
            if config.get("clean_predictions") or config.get("clean_runtime")
            else "not_configured"
        ),
        "candidate_proteins": len(clean_candidates),
        "selected": 0,
        "mapped_genes": 0,
        "top_fraction": float(
            config.get("clean_top_fraction", DEFAULT_CLEAN_TOP_FRACTION)
        ),
        "input_fasta": str(clean_fasta),
        "input_tsv": str(clean_tsv),
        "source": "CLEAN",
    }
    prediction_path = Path(config["clean_predictions"]) if config.get("clean_predictions") else None
    if prediction_path is None and config.get("clean_runtime") and clean_candidates:
        phase("clean_prediction")
        prediction_path = run_clean_predictions(
            clean_fasta=clean_fasta,
            out=out,
            runtime=Path(config["clean_runtime"]),
            python_executable=Path(config.get("clean_python") or sys.executable),
            timeout=int(config.get("clean_timeout", 3600)),
        )
        manifest["clean_prediction"].update(
            {
                "mode": "runtime",
                "runtime": str(Path(config["clean_runtime"]).resolve()),
                "runtime_python": str(
                    Path(config.get("clean_python") or sys.executable).resolve()
                ),
            }
        )
    if prediction_path is not None:
        prediction_ids = clean_prediction_input_ids(prediction_path)
        input_ids = {str(protein_id) for protein_id, _ in proteins}
        candidate_ids = input_ids - ncbi_ec_ids
        overlap = prediction_ids & input_ids
        candidate_overlap = prediction_ids & candidate_ids
        manifest["clean_prediction"].update(
            {
                "prediction_ids": len(prediction_ids),
                "input_protein_ids": len(input_ids),
                "id_overlap": len(overlap),
                "candidate_id_overlap": len(candidate_overlap),
                "prediction_file_sha256": _sha256(prediction_path),
                "input_fasta_sha256": _sha256(clean_fasta),
            }
        )
        no_id_overlap = bool(prediction_ids and not overlap)
        if config.get("clean_predictions_require_overlap") and no_id_overlap:
            manifest["clean_prediction"].update(
                status="no_id_overlap",
                rejection_reason="prediction_ids_do_not_match_input_proteins",
            )
            raise ToolError(
                "CLEAN predictions contain no protein IDs from the current input; "
                "run CLEAN on this genome's clean-input-no-ncbi.faa"
            )
        if no_id_overlap:
            # A prediction table for another genome is not an empty CLEAN
            # result: keep the distinction in the manifest and do not pass
            # unrelated rows to the importer.  Runtime-generated predictions
            # normally avoid this branch because they use this FASTA.
            clean_rows = []
        else:
            phase("clean_prediction")
            clean_rows = import_clean_predictions(
                prediction_path,
                proteins,
                ncbi_ec_ids,
                float(config.get("clean_top_fraction", DEFAULT_CLEAN_TOP_FRACTION)),
            )
        evidence.extend(clean_rows)
        write_json(out / "clean-predictions-selected.json", clean_rows)
        selected_status = (
            "no_id_overlap"
            if no_id_overlap
            else "selected"
            if clean_rows
            else "no_valid_prediction"
            if clean_candidates
            else "not_applicable"
        )
        manifest["clean_prediction"].update(
            {
                "selected": len(clean_rows),
                "predictions": str(prediction_path.resolve()),
                "status": selected_status,
            }
        )
    elif not clean_candidates:
        manifest["clean_prediction"]["status"] = "not_applicable"
    clean_input_ids = {str(row["gene_id"]) for row in clean_rows}
    gene_ids = {rid: f"g{i + 1:06d}" for i, (rid, _) in enumerate(proteins)}
    write_json(out / "gene_id_map.json", gene_ids)
    for row in evidence:
        row["input_gene_id"] = row["gene_id"]
        row["gene_id"] = gene_ids[row["gene_id"]]
    write_json(out / "annotation.json", evidence)
    return clean_input_ids, gene_ids, clean_rows, ncbi_ec_ids


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
