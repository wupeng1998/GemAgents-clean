"""Mechanically migrated deterministic metabolic implementation.
Layer facades provide the public organization while functions move incrementally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from GemAgents import solver_result as _solver_result
from GemAgents.metabolic import qc as _metabolic_qc
from GemAgents.metabolic.contracts import (
    ReconstructionContext,
)
from GemAgents.metabolic.evidence import ledger, restricted_path
from GemAgents.metabolic.io import ArtifactStore, metabolic_hash, metabolic_json

# Historical ``GemAgents.tools`` imports resolve through this compatibility
# module; keep the QC symbols available while their implementation lives in
# the dedicated qc package.
Auditor = _metabolic_qc.Auditor
Probe = _metabolic_qc.Probe
Task = _metabolic_qc.Task
mass_probe = _metabolic_qc.mass_probe
repair = _metabolic_qc.repair
solve_with_classification = _solver_result.solve_with_classification

# Deterministic metabolic reconstruction and independent CER validation tools.


def metabolic_validate_options(config: dict) -> None:
    """Compatibility facade for deterministic option validation."""
    from GemAgents.metabolic.contracts import (
        metabolic_validate_options as _metabolic_validate_options,
    )

    return _metabolic_validate_options(config)


def metabolic_start(
    runner: Any,
    config_path: str,
    *,
    contract: dict[str, object] | None = None,
) -> str:
    """Compatibility facade for background reconstruction job submission."""
    from GemAgents.metabolic.jobs.facade import metabolic_start as _metabolic_start

    return _metabolic_start(
        runner,
        config_path,
        contract=contract,
        write_json_fn=metabolic_json,
        popen_fn=subprocess.Popen,
        validate_options_fn=metabolic_validate_options,
    )

def metabolic_status(runner: Any, job_id: str) -> str:
    # Compatibility import for callers that historically used ``legacy``.
    from GemAgents.metabolic.jobs.runtime import metabolic_status as _metabolic_status

    return _metabolic_status(runner, job_id)


def metabolic_resume(runner: Any, job_id: str) -> str:
    """Compatibility facade for verified checkpoint worker resumption."""
    from GemAgents.metabolic.jobs.facade import metabolic_resume as _metabolic_resume

    return _metabolic_resume(runner, job_id)


def metabolic_cancel(
    runner: Any,
    job_id: str,
    *,
    terminate: bool = False,
    grace_seconds: float = 5.0,
) -> str:
    # Compatibility import for callers that historically used ``legacy``.
    from GemAgents.metabolic.jobs.runtime import metabolic_cancel as _metabolic_cancel

    return _metabolic_cancel(
        runner,
        job_id,
        terminate=terminate,
        grace_seconds=grace_seconds,
    )


def metabolic_fasta(path: Path, kind: str) -> list[tuple[str, str]]:
    """Compatibility facade for the extracted sequence leaf module."""
    from GemAgents.metabolic.sequence import metabolic_fasta as _metabolic_fasta

    return _metabolic_fasta(path, kind)


def metabolic_write_fasta(records: list[tuple[str, str]], path: Path) -> None:
    from GemAgents.metabolic.sequence import metabolic_write_fasta as _metabolic_write_fasta

    _metabolic_write_fasta(records, path)


def metabolic_detect_input(path: Path, requested: str = "auto") -> tuple[str, str]:
    from GemAgents.metabolic.sequence import metabolic_detect_input as _metabolic_detect_input

    return _metabolic_detect_input(path, requested)


def metabolic_download(url: str, path: Path) -> None:
    """Compatibility facade for bounded public reference downloads."""
    from GemAgents.metabolic.network import metabolic_download as _metabolic_download

    _metabolic_download(url, path)


def metabolic_prepare_hmms(directory: Path, *, download: bool = True) -> dict:
    """Compatibility facade for the extracted NCBI HMM preparation leaf."""
    from GemAgents.metabolic.hmm import metabolic_prepare_hmms as _prepare_hmms

    return _prepare_hmms(directory, download=download)


def metabolic_hmm_annotate(faa: Path, directory: Path, cpus: int, out: Path) -> list[dict]:
    """Compatibility facade for calibrated NCBI HMM annotation."""
    from GemAgents.metabolic.hmm import metabolic_hmm_annotate as _hmm_annotate

    return _hmm_annotate(faa, directory, cpus, out)


def metabolic_import_ncbi(
    input_path: Path, kind: str, gbk: Path, out: Path
) -> tuple[Path, list[dict]]:
    """Compatibility facade for the extracted GenBank import leaf."""
    from GemAgents.metabolic.annotation_import import metabolic_import_ncbi as _import_ncbi

    return _import_ncbi(input_path, kind, gbk, out)


def metabolic_predict_genes(input_path: Path, out: Path, genetic_code: int) -> Path:
    """Compatibility facade for the extracted Pyrodigal annotation leaf."""
    from GemAgents.metabolic.annotation_import import metabolic_predict_genes as _predict_genes

    return _predict_genes(input_path, out, genetic_code)


PGAP_VERSION = "2026-06-18.build8602"
PGAP_DISTRO = "GemAgents-PGAP"
PGAP_LINUX_HOME = "/opt/gemagents-pgap"


def pgap_prepare_windows(assets: Path) -> None:
    """Compatibility facade for Windows PGAP prerequisite preparation."""
    from GemAgents.metabolic.pgap import pgap_prepare_windows as _pgap_prepare_windows

    return _pgap_prepare_windows(
        assets,
        download_fn=metabolic_download,
        hash_fn=metabolic_hash,
        json_fn=metabolic_json,
    )


def pgap_prepare_ubuntu(assets: Path) -> None:
    """Compatibility facade for Ubuntu PGAP image preparation."""
    from GemAgents.metabolic.pgap import pgap_prepare_ubuntu as _pgap_prepare_ubuntu

    return _pgap_prepare_ubuntu(
        assets,
        download_fn=metabolic_download,
        hash_fn=metabolic_hash,
        json_fn=metabolic_json,
    )


def pgap_decode(data: bytes) -> str:
    """Compatibility facade for PGAP subprocess output decoding."""
    from GemAgents.metabolic.pgap import pgap_decode as _pgap_decode

    return _pgap_decode(data)


def pgap_call(command: list[str], *, log: Path | None = None, timeout: int = 120) -> str:
    """Compatibility facade for the argument-vector PGAP subprocess runner."""
    from GemAgents.metabolic.pgap import pgap_call as _pgap_call

    return _pgap_call(command, log=log, timeout=timeout, decode_fn=pgap_decode)


def pgap_wsl_prefix(distro: str) -> list[str]:
    """Compatibility facade for a validated WSL argument prefix."""
    from GemAgents.metabolic.pgap import pgap_wsl_prefix as _pgap_wsl_prefix

    return _pgap_wsl_prefix(distro)


def pgap_runtime_config(workspace: Path, config_path: str | None = None) -> dict:
    """Compatibility facade for side-effect-free PGAP runtime validation."""
    from GemAgents.metabolic.pgap import pgap_runtime_config as _pgap_runtime_config

    return _pgap_runtime_config(workspace, config_path)


def pgap_check(workspace: Path) -> dict:
    """Compatibility facade for the read-only PGAP readiness check."""
    from GemAgents.metabolic.pgap import pgap_check as _pgap_check

    return _pgap_check(workspace, call_fn=pgap_call)


def pgap_setup(workspace: Path) -> dict:
    """Compatibility facade for extracted PGAP installation orchestration."""
    from GemAgents.metabolic.pgap import pgap_setup as _pgap_setup

    return _pgap_setup(
        workspace,
        version=PGAP_VERSION,
        distro=PGAP_DISTRO,
        linux_home=PGAP_LINUX_HOME,
        call_fn=pgap_call,
        download_fn=metabolic_download,
        hash_fn=metabolic_hash,
        json_fn=metabolic_json,
        prepare_windows_fn=pgap_prepare_windows,
        prepare_ubuntu_fn=pgap_prepare_ubuntu,
        runtime_check_fn=pgap_check,
    )


def metabolic_run_pgap(
    input_path: Path,
    out: Path,
    config: dict,
    workspace: Path | None = None,
) -> Path:
    """Compatibility facade for the extracted PGAP execution leaf."""
    from GemAgents.metabolic.pgap import pgap_run as _pgap_run

    adapter_register_fn = None
    adapter_clear_fn = None
    worker_job_id = os.environ.get("GEMAGENTS_JOB_ID")
    worker_attempt_id = os.environ.get("GEMAGENTS_JOB_ATTEMPT_ID")
    if workspace is not None and worker_job_id and worker_attempt_id:
        from GemAgents.metabolic.jobs.lifecycle import JobLedger

        worker_ledger = JobLedger(workspace)

        def register_adapter(adapter: dict) -> None:
            worker_ledger.register_adapter(
                worker_job_id,
                attempt_id=worker_attempt_id,
                kind=str(adapter["kind"]),
                identity=str(adapter["identity"]),
                metadata={
                    key: value
                    for key, value in adapter.items()
                    if key not in {"kind", "identity"}
                },
            )

        def clear_adapter(adapter: dict) -> None:
            worker_ledger.clear_adapter(
                worker_job_id,
                attempt_id=worker_attempt_id,
                identity=str(adapter["identity"]),
            )

        adapter_register_fn = register_adapter
        adapter_clear_fn = clear_adapter

    return _pgap_run(
        input_path,
        out,
        config,
        workspace,
        runtime_config_fn=pgap_runtime_config,
        fasta_fn=metabolic_fasta,
        write_fasta_fn=metabolic_write_fasta,
        wsl_prefix_fn=pgap_wsl_prefix,
        call_fn=pgap_call,
        json_fn=metabolic_json,
        which_fn=shutil.which,
        adapter_register_fn=adapter_register_fn,
        adapter_clear_fn=adapter_clear_fn,
    )


def metabolic_memote_worker(model_path: Path, out: Path, solver_timeout: int = 10) -> dict:
    """Compatibility facade for the extracted MEMOTE worker."""
    from GemAgents.metabolic.qc.memote import (
        metabolic_memote_worker as _metabolic_memote_worker,
    )

    return _metabolic_memote_worker(
        model_path,
        out,
        solver_timeout,
        hash_fn=metabolic_hash,
        json_fn=metabolic_json,
    )


def metabolic_memote(model_path: Path, out: Path, config: dict | None = None) -> dict:
    """Compatibility facade for isolated MEMOTE orchestration."""
    from GemAgents.metabolic.qc.memote import metabolic_memote as _metabolic_memote

    return _metabolic_memote(
        model_path,
        out,
        config,
        hash_fn=metabolic_hash,
        json_fn=metabolic_json,
    )


def metabolic_aliases(value: str, namespace: str = "BiGG") -> list[str]:
    """Compatibility facade for reaction-library alias normalization."""
    from GemAgents.metabolic.library.normalization import metabolic_aliases as _metabolic_aliases

    return _metabolic_aliases(value, namespace)


def metabolic_compartment(value: str) -> str:
    """Compatibility facade for reaction-library compartment normalization."""
    from GemAgents.metabolic.library.normalization import (
        metabolic_compartment as _metabolic_compartment,
    )

    return _metabolic_compartment(value)


def metabolic_equation_key(stoichiometry: dict[str, float]) -> tuple:
    """Compatibility facade for exact reaction equation normalization."""
    from GemAgents.metabolic.library.normalization import (
        metabolic_equation_key as _metabolic_equation_key,
    )

    return _metabolic_equation_key(stoichiometry)


def metabolic_merge_libraries(bigg, seed, compounds: dict, reactions: dict):
    """Compatibility facade for conservative reaction-library merging."""
    from GemAgents.metabolic.library.merge import metabolic_merge_libraries as _merge_libraries

    return _merge_libraries(bigg, seed, compounds, reactions)


def metabolic_quality_control_library(model):
    """Compatibility facade for reaction-library quality control."""
    from GemAgents.metabolic.library.quality import (
        metabolic_quality_control_library as _quality_control_library,
    )

    return _quality_control_library(model)


def metabolic_apply_bigg_direction_union(model, model_directory: Path) -> tuple[dict, list[dict]]:
    """Compatibility facade for public BiGG direction-union processing."""
    from GemAgents.metabolic.library.direction_union import (
        metabolic_apply_bigg_direction_union as _direction_union,
    )

    return _direction_union(model, model_directory)


def metabolic_prepare_library(workspace: Path, out: Path, config: dict | None = None) -> dict:
    """Compatibility facade for auditable reaction-library construction."""
    from GemAgents.metabolic.library.prepare import (
        metabolic_prepare_library as _prepare_library,
    )

    return _prepare_library(workspace, out, config)


def metabolic_universe(config: dict):
    """Compatibility facade for reaction-universe selection."""
    from GemAgents.metabolic.library.universe import metabolic_universe as _metabolic_universe

    return _metabolic_universe(config)

def metabolic_import_clean_predictions(
    path: Path,
    proteins: list[tuple[str, str]],
    ncbi_ec_genes: set[str],
    top_fraction: float = 0.30,
) -> list[dict]:
    """Compatibility facade for the extracted deterministic parser."""
    from GemAgents.metabolic.predictions import (
        metabolic_import_clean_predictions as _metabolic_import_clean_predictions,
    )

    return _metabolic_import_clean_predictions(path, proteins, ncbi_ec_genes, top_fraction)


def metabolic_map_evidence(
    model, evidence: list[dict], *, allow_ambiguous_gpr: bool = False
) -> dict[str, dict]:
    """Compatibility facade for the extracted evidence mapper."""
    from GemAgents.metabolic.evidence.mapping import metabolic_map_evidence as _map_evidence

    return _map_evidence(model, evidence, allow_ambiguous_gpr=allow_ambiguous_gpr)


def metabolic_mapping_audit(model, evidence: list[dict], mapped: dict[str, dict]) -> dict:
    """Compatibility facade for the mapping-loss diagnostic report."""
    from GemAgents.metabolic.evidence.mapping import (
        metabolic_mapping_audit as _mapping_audit,
    )

    return _mapping_audit(model, evidence, mapped)


def _metabolic_evidence_status(entry: dict | None) -> str:
    from GemAgents.metabolic.evidence.mapping import metabolic_evidence_status

    return metabolic_evidence_status(entry)


def metabolic_reaction_evidence_ledger(model) -> list[dict]:
    """Compatibility facade for the extracted evidence ledger."""
    from GemAgents.metabolic.evidence.mapping import (
        metabolic_reaction_evidence_ledger as _reaction_evidence_ledger,
    )

    return _reaction_evidence_ledger(model)


def metabolic_set_medium(model, medium: object) -> dict[str, float]:
    """Compatibility facade for medium preset and alias resolution."""
    from GemAgents.metabolic.media.selection import metabolic_set_medium as _set_medium

    return _set_medium(model, medium)


def metabolic_build_model(universal, universe_path: Path, mapped: dict, config: dict, out: Path):
    """Compatibility facade for evidence-guided model construction."""
    from GemAgents.metabolic.reconstruction.build import metabolic_build_model as _build_model

    return _build_model(universal, universe_path, mapped, config, out)


def metabolic_attach_gene_annotations(
    model, evidence: list[dict], proteins: list[tuple[str, str]], gene_ids: dict[str, str]
) -> dict[str, int]:
    """Compatibility facade for deterministic model gene annotation."""
    from GemAgents.metabolic.reconstruction.annotations import (
        metabolic_attach_gene_annotations as _attach_gene_annotations,
    )

    return _attach_gene_annotations(model, evidence, proteins, gene_ids)


def metabolic_enrich_memote_annotations(model) -> dict[str, int]:
    """Compatibility facade for portable model SBO annotations."""
    from GemAgents.metabolic.reconstruction.annotations import (
        metabolic_enrich_memote_annotations as _enrich_memote_annotations,
    )

    return _enrich_memote_annotations(model)


def _native_sequence_sketch(records, alphabet: str, k: int = 21, size: int = 2048) -> list[int]:
    """Compatibility facade for deterministic biomass sequence sketches."""
    from GemAgents.metabolic.biomass.selection import sequence_sketch

    return sequence_sketch(records, alphabet, k, size)


def _native_sketch_similarity(left: list[int], right: list[int]) -> float:
    """Compatibility facade for biomass sketch similarity."""
    from GemAgents.metabolic.biomass.selection import sketch_similarity

    return sketch_similarity(left, right)


def _native_reference_proteins(path: Path) -> list[dict]:
    """Compatibility facade for public-template reference proteins."""
    from GemAgents.metabolic.biomass.reference import reference_proteins

    return reference_proteins(path)


def _native_load_source_model(path: Path):
    """Compatibility facade for source-model loading."""
    from GemAgents.metabolic.library.source_io import (
        load_source_model as _load_source_model,
    )

    return _load_source_model(path)


def _metabolic_is_empirical_biomass_assembly(
    reaction, biomass_reactants: set[str]
) -> bool:
    """Compatibility facade for empirical biomass assembly classification."""
    from GemAgents.metabolic.biomass.assembly import is_empirical_biomass_assembly

    return is_empirical_biomass_assembly(reaction, biomass_reactants)


def _metabolic_is_empirical_pool_reaction(
    reaction, required_products: set[str]
) -> bool:
    """Compatibility facade for empirical biomass pool classification."""
    from GemAgents.metabolic.biomass.assembly import is_empirical_pool_reaction

    return is_empirical_pool_reaction(reaction, required_products)


def metabolic_prepare_biomass_library(
    workspace: Path, spec_path: Path, out: Path, reaction_library: Path
) -> dict:
    """Compatibility facade for deterministic biomass catalog compilation."""
    from GemAgents.metabolic.biomass.compiler import compile_biomass_library

    return compile_biomass_library(
        workspace,
        spec_path,
        out,
        reaction_library,
        native_universe_fn=lambda config: metabolic_native_universe(config),
        compartment_fn=metabolic_compartment,
        load_source_model_fn=_native_load_source_model,
        reference_proteins_fn=_native_reference_proteins,
        fasta_fn=metabolic_fasta,
        hash_fn=metabolic_hash,
        write_json_fn=metabolic_json,
        sequence_sketch_fn=_native_sequence_sketch,
        restricted_path_fn=restricted_path,
        is_assembly_fn=_metabolic_is_empirical_biomass_assembly,
        is_pool_fn=_metabolic_is_empirical_pool_reaction,
    )

def metabolic_discover_public_biomass_registry(
    out: Path, download_models: bool = False
) -> dict:
    """Compatibility facade for the public biomass registry census."""
    from GemAgents.metabolic.biomass.registry import (
        metabolic_discover_public_biomass_registry as _discover_registry,
    )

    return _discover_registry(out, download_models)

def _native_choose_biomass(
    catalog: dict, input_path: Path, kind: str, config: dict
) -> tuple[dict, dict]:
    """Compatibility facade for biomass template selection."""
    from GemAgents.metabolic.biomass.selection import choose_biomass

    return choose_biomass(catalog, input_path, kind, config)


def _native_identity(query: str, reference: str) -> tuple[float, float]:
    from GemAgents.metabolic.reconstruction.reference_support import sequence_identity

    return sequence_identity(query, reference)


def _native_compile_gpr(
    rule: str, reference_genes: dict, evidence: list[dict], proteins: dict, config: dict
) -> tuple[str, dict]:
    from GemAgents.metabolic.reconstruction.reference_support import compile_gpr

    return compile_gpr(rule, reference_genes, evidence, proteins, config)


def _metabolic_normalize_bigg_compartments(model) -> int:
    """Compatibility facade for compartment metadata normalization."""
    from GemAgents.metabolic.reconstruction.native_universe import normalize_bigg_compartments

    return normalize_bigg_compartments(model)


def metabolic_native_universe(config: dict):
    """Compatibility facade for native reaction-universe loading."""
    from GemAgents.metabolic.reconstruction.native_universe import (
        metabolic_native_universe as _native_universe,
    )

    return _native_universe(config)


def _metabolic_oxygen_exchange_ids(model) -> set[str]:
    """Compatibility facade for oxygen exchange identification."""
    from GemAgents.metabolic.reconstruction.support import oxygen_exchange_ids

    return oxygen_exchange_ids(model)


def _metabolic_reference_growth_scenarios(
    source_path: Path, medium: object, minimum: float
) -> list[dict]:
    """Compatibility facade for reference growth scenarios."""
    from GemAgents.metabolic.reconstruction.support import reference_growth_scenarios

    return reference_growth_scenarios(source_path, medium, minimum)


def _metabolic_prune_condition_redundancy(
    model, biomass_id: str, additions: list[str], scenarios: list[dict], tolerance: float
) -> list[str]:
    """Compatibility facade for condition redundancy pruning."""
    from GemAgents.metabolic.reconstruction.support import prune_condition_redundancy

    return prune_condition_redundancy(model, biomass_id, additions, scenarios, tolerance)

def _native_add_reference_iML1515_support(
    universal,
    source_path: Path,
    reference_genes: dict,
    evidence: list[dict],
    proteins: dict,
    config: dict,
    reference_prefix: str = "REF_iML1515_",
    source_biomass_id: str = "BIOMASS_Ec_iML1515_core_75p37M",
) -> tuple[set[str], dict[str, dict], dict[str, dict], dict[str, object]]:
    """Compatibility facade for the public reference reaction bridge."""
    from GemAgents.metabolic.reconstruction.reference_bridge import (
        add_reference_iml1515_support,
    )

    return add_reference_iml1515_support(
        universal,
        source_path,
        reference_genes,
        evidence,
        proteins,
        config,
        reference_prefix,
        source_biomass_id,
    )
def _native_add_biomass_product_drains(model, biomass) -> set[str]:
    """Compatibility facade for biomass product drains."""
    from GemAgents.metabolic.reconstruction.support import add_biomass_product_drains

    return add_biomass_product_drains(model, biomass)


def _native_template_support_reaction_id(universal, support: dict, template_id: str) -> str:
    """Compatibility facade for template support reaction IDs."""
    from GemAgents.metabolic.reconstruction.support import template_support_reaction_id

    return template_support_reaction_id(universal, support, template_id)


def metabolic_native_build_model(
    universal,
    universe_path: Path,
    mapped: dict,
    evidence: list[dict],
    config: dict,
    out: Path,
    *,
    context: ReconstructionContext | None = None,
):
    """Compatibility facade for the extracted native build orchestration."""
    from GemAgents.metabolic.reconstruction.facade import (
        metabolic_native_build_model as _native_build,
    )
    from GemAgents.metabolic.reconstruction.initial_model import close_transport_and_annotate

    return _native_build(
        universal,
        universe_path,
        mapped,
        evidence,
        config,
        out,
        context=context,
        choose_biomass_fn=_native_choose_biomass,
        add_reference_support_fn=_native_add_reference_iML1515_support,
        add_biomass_product_drains_fn=_native_add_biomass_product_drains,
        fasta_fn=metabolic_fasta,
        write_json_fn=metabolic_json,
        reference_growth_scenarios_fn=_metabolic_reference_growth_scenarios,
        oxygen_exchange_ids_fn=_metabolic_oxygen_exchange_ids,
        close_transport_fn=close_transport_and_annotate,
        biomass_precursor_audit_fn=_metabolic_biomass_precursor_audit,
        evidence_status_fn=_metabolic_evidence_status,
        solve_fn=solve_with_classification,
    )


def _metabolic_reference_growth(config: dict) -> tuple[float, str] | None:
    """Compatibility facade for the extracted growth-condition leaf."""
    from GemAgents.metabolic.reconstruction.conditions import reference_growth

    return reference_growth(config)


def _metabolic_growth_tasks(model, biomass: str, minimum: float) -> tuple[Any, ...]:
    """Compatibility facade for the extracted growth-condition leaf."""
    from GemAgents.metabolic.reconstruction.conditions import growth_tasks

    return growth_tasks(model, biomass, minimum)


def _metabolic_biomass_precursor_audit(
    model, biomass: str, *, timeout: int = 30, tolerance: float = 1e-7
) -> dict:
    """Compatibility facade for the extracted reconstruction audit leaf."""
    from GemAgents.metabolic.reconstruction.precursor_audit import (
        metabolic_biomass_precursor_audit,
    )

    return metabolic_biomass_precursor_audit(
        model, biomass, timeout=timeout, tolerance=tolerance
    )


def metabolic_quality(model, biomass: str, config: dict) -> tuple[object, dict]:
    """Compatibility facade for the extracted reconstruction QC leaf."""
    from GemAgents.metabolic.reconstruction.quality import metabolic_quality as _metabolic_quality

    return _metabolic_quality(
        model,
        biomass,
        config,
        growth_tasks_fn=_metabolic_growth_tasks,
        reference_growth_fn=_metabolic_reference_growth,
        precursor_audit_fn=_metabolic_biomass_precursor_audit,
        repair_fn=repair,
    )


class _WorkerCancelled(Exception):
    """Compatibility marker for historical cancellation imports."""


def metabolic_pipeline(
    config: dict,
    workspace: Path,
    *,
    contract: Any | None = None,
) -> dict:
    """Compatibility facade for deterministic reconstruction orchestration."""
    from GemAgents.metabolic.contracts import STRING_OPTIONS
    from GemAgents.metabolic.jobs.contracts import compile_contract
    from GemAgents.metabolic.jobs.lifecycle import JobLedger
    from GemAgents.metabolic.reconstruction.pipeline_resume import (
        load_reaction_mapping_stage,
    )
    from GemAgents.metabolic.reconstruction.pipeline_runner import (
        metabolic_pipeline as _metabolic_pipeline,
    )

    return _metabolic_pipeline(
        config,
        workspace,
        contract=contract,
        callbacks={
            "validate_options": metabolic_validate_options,
            "compile_contract": compile_contract,
            "restricted_path": restricted_path,
            "string_options": STRING_OPTIONS,
            "artifact_store_factory": ArtifactStore,
            "source_ledger": ledger,
            "hash_path": metabolic_hash,
            "workflow_path": Path(__file__),
            "job_ledger_factory": JobLedger,
            "write_json": metabolic_json,
            "detect_input": metabolic_detect_input,
            "run_pgap": metabolic_run_pgap,
            "import_ncbi": metabolic_import_ncbi,
            "predict_genes": metabolic_predict_genes,
            "write_fasta": metabolic_write_fasta,
            "fasta": metabolic_fasta,
            "hmm_annotate": metabolic_hmm_annotate,
            "import_clean_predictions": metabolic_import_clean_predictions,
            "native_universe": metabolic_native_universe,
            "universe": metabolic_universe,
            "map_evidence": metabolic_map_evidence,
            "mapping_audit": metabolic_mapping_audit,
            "native_build": metabolic_native_build_model,
            "build_model": metabolic_build_model,
            "attach_gene_annotations": metabolic_attach_gene_annotations,
            "enrich_memote_annotations": metabolic_enrich_memote_annotations,
            "quality": metabolic_quality,
            "reaction_evidence_ledger": metabolic_reaction_evidence_ledger,
            "memote": metabolic_memote,
            "load_source_model": _native_load_source_model,
            "load_reaction_mapping_stage": load_reaction_mapping_stage,
        },
    )
