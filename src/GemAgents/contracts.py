"""Strict reconstruction inputs and independent execution/validation status fields."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ALLOW_AMBIGUOUS_EC_GPR = True
# The strict view is the safe operational default. The full union remains
# available for explicit catalog audits and comparison runs.
DEFAULT_REACTION_LIBRARY_MODE = "strict"
DEFAULT_CLEAN_TOP_FRACTION = 0.30
DEFAULT_REFERENCE_SUPPORT_PATH = "data/public_biomass_sources/bigg_models/iML1515.json.gz"
DEFAULT_REFERENCE_PROTEIN_FASTA = "carveme/carveme/data/benchmark/fasta/Ecoli_K12_MG1655.faa"


def default_reaction_library_path(workspace: Path | None) -> str:
    """Select the newest bundled prokaryotic reaction library.

    ``reaction_library_v6`` remains the compatibility fallback for a checkout
    that has not materialized the repaired library yet.  A versioned path is
    selected only when its manifest and operational SBML views are present, so
    temporary test workspaces and older installations keep their existing behavior.
    Policy-restricted candidates are skipped even when their files are present;
    callers must opt into those sources explicitly through a reviewed route.
    """
    if workspace is not None:
        # Keep policy enforcement at the same boundary as explicit user paths.
        # Import lazily to avoid a module-level contracts/provenance cycle.
        from GemAgents.provenance import restricted_path

        for candidate in (
            "data/reaction_library_v8_pear_ec_20260924",
            "data/reaction_library_v8_ec_alias_20260924",
            "data/reaction_library_v8",
            "data/reaction_library_v7",
        ):
            library = workspace / candidate
            if restricted_path(library):
                continue
            if (
                (library / "manifest.json").is_file()
                and (library / "universe_full.xml.gz").is_file()
                and (library / "universe.xml.gz").is_file()
            ):
                return candidate
    return "data/reaction_library_v6"


def default_ec_alias_source(workspace: Path | None) -> str | None:
    """Find the user-authorized Protokaryon EC snapshot when it is available."""
    if workspace is None:
        return None
    candidates = (
        workspace / "data/Protokaryon/ec-code_annotation.csv",
        workspace.parent / "pear/files/Protokaryon/ec-code_annotation.csv",
    )
    return next((str(path.resolve()) for path in candidates if path.is_file()), None)


def default_biomass_library_path(workspace: Path | None) -> str:
    """Select the repaired shared prokaryotic biomass catalog when present."""
    if workspace is not None:
        repaired = workspace / "data/prokaryotic_biomass_library_v2"
        if (repaired / "manifest.json").is_file() and (repaired / "catalog.json").is_file():
            return "data/prokaryotic_biomass_library_v2"
    return "data/prokaryotic_biomass_library"


def _default_existing_path(workspace: Path | None, value: str) -> Path | None:
    """Return a bundled default asset only when it exists in this workspace."""
    if workspace is None:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_file() else None


def default_clean_predictions_path(workspace: Path | None) -> Path | None:
    """Return an explicitly bundled CLEAN table, if one exists.

    This compatibility helper never selects the historical iML1515 table;
    default reconstruction runs invoke CLEAN on their own generated FASTA.
    """
    if workspace is None:
        return None
    candidates = (
        workspace / "data/clean_predictions.csv",
        workspace / "data/clean_predictions.tsv",
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def default_clean_runtime(workspace: Path | None) -> tuple[Path, Path] | None:
    """Locate the optional CLEAN runtime, without selecting old predictions."""
    from GemAgents.metabolic.reconstruction.clean_runtime import discover_clean_runtime

    return discover_clean_runtime(workspace)


def apply_reconstruction_defaults(
    config: dict[str, Any], workspace: Path | None = None
) -> dict[str, Any]:
    """Materialize the default reconstruction track without overwriting choices.

    The default track follows the reference-assisted comparison workflow. CLEAN
    is run on the current input when a local runtime is available; historical
    prediction tables are never selected implicitly.
    """
    effective = copy.deepcopy(config)
    effective.setdefault("reaction_library_mode", DEFAULT_REACTION_LIBRARY_MODE)
    effective.setdefault("clean_top_fraction", DEFAULT_CLEAN_TOP_FRACTION)

    if (
        "clean_predictions" not in effective
        and not effective.get("disable_clean_predictions_default")
    ):
        input_value = effective.get("input")
        input_path = Path(str(input_value)).expanduser() if input_value else None
        if input_path is not None and workspace is not None and not input_path.is_absolute():
            input_path = workspace / input_path
        runtime = (
            default_clean_runtime(workspace)
            if input_path is not None and input_path.is_file()
            else None
        )
        if runtime is not None:
            clean_app, clean_python = runtime
            effective.setdefault("clean_runtime", str(clean_app))
            effective.setdefault("clean_python", str(clean_python))

    if effective.get("engine", "native") != "native":
        return effective
    if effective.get("reference_support") is False:
        return effective
    if effective.get("reference_scaffold") is False:
        return effective
    if effective.get("evaluation_track") in {"de_novo_public", "biomass_controlled"}:
        return effective

    reference_path = _default_existing_path(workspace, DEFAULT_REFERENCE_SUPPORT_PATH)
    protein_path = _default_existing_path(workspace, DEFAULT_REFERENCE_PROTEIN_FASTA)
    if reference_path is None or protein_path is None:
        return effective

    effective.setdefault("reference_support", True)
    effective.setdefault("reference_support_path", str(reference_path))
    effective.setdefault("reference_protein_fasta", str(protein_path))
    effective.setdefault("evaluation_track", "reference_assisted")
    effective.setdefault("reference_scaffold", True)
    # The comparison workflow carries the full reference network forward.
    effective.setdefault("reference_scaffold_source_only", False)
    return effective

BOOLEAN_OPTIONS = frozenset(
    {
        "allow_non_growing_draft",
        "allow_ambiguous_ec_gpr",
        "allow_unverified_gapfill",
        "clean_predictions_require_overlap",
        "disable_clean_predictions_default",
        "memote",
        "reference_growth_ceiling",
        "reference_scaffold",
        "reference_scaffold_source_only",
        "reference_support",
    }
)
INTEGER_OPTIONS = frozenset(
    {
        "cpus",
        "clean_timeout",
        "gapfill_diagnostic_timeout",
        "genetic_code",
        "max_edits",
        "memote_solver_timeout",
        "memote_timeout",
        "qc_timeout",
        "solver_timeout",
        "timeout",
    }
)
FRACTION_OPTIONS = frozenset(
    {"biomass_min_similarity", "clean_top_fraction", "gpr_min_coverage", "gpr_min_identity"}
)
LIST_OPTIONS = frozenset({"growth_rescue_reactions"})
STRING_OPTIONS = frozenset(
    {
        "annotation_gbk",
        "biomass_library",
        "biomass_template",
        "clean_predictions",
        "clean_python",
        "clean_runtime",
        "ec_alias_source",
        "hmm_dir",
        "input",
        "medium_file",
        "modelseed_reactions",
        "organism",
        "output",
        "pgap_config",
        "pgap_memory",
        "pgap_output",
        "pgap_script",
        "reaction_library",
        "reference_support_path",
        "reference_protein_fasta",
        "universe",
    }
)
ENUM_OPTIONS = {
    "evaluation_track": (
        "de_novo_public",
        "biomass_controlled",
        "reference_assisted",
        "authorized_private",
    ),
    "input_type": ("auto", "faa", "fna"),
    "engine": ("native", "carveme", "reconstructor"),
    "annotation": ("auto", "pgap", "ncbi-import", "ncbi-hmm", "pyrodigal-ncbi-hmm"),
    "quality": ("audit", "repair"),
    "reaction_library_mode": ("full", "strict"),
    "kingdom": ("bacteria", "archaea"),
    "gram": ("positive", "negative", "unspecified"),
}


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str):
            raise ValueError("Configuration keys must be strings")
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


class UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate keys, including overlapping YAML merge mappings."""

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        return unique_pairs(
            (self.construct_object(k, deep=deep), self.construct_object(v, deep=deep))
            for k, v in node.value
        )


def finite_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Configuration numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            finite_values(item)
    elif isinstance(value, list):
        for item in value:
            finite_values(item)


def load_configuration(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    value = (
        yaml.load(text, Loader=UniqueSafeLoader)
        if path.suffix.lower() in {".yaml", ".yml"}
        else json.loads(text, object_pairs_hook=unique_pairs)
    )
    finite_values(value)
    if not isinstance(value, dict):
        raise ValueError("Reconstruction config must be an object")
    return value


def number(value):
    # Avoid float conversion overflow for arbitrarily large JSON integers.
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True)
class ReconstructionConfig:
    """Validated snapshot; to_dict supplies a separate mutable execution copy."""

    values: dict[str, Any]

    def __post_init__(self):
        config = self.values
        if not isinstance(config, dict):
            raise ValueError("Reconstruction config must be an object")
        allowed = (
            BOOLEAN_OPTIONS
            | INTEGER_OPTIONS
            | FRACTION_OPTIONS
            | STRING_OPTIONS
            | set(ENUM_OPTIONS)
            | LIST_OPTIONS
            | {"medium", "min_growth"}
        )
        unknown = set(config) - allowed
        if unknown:
            raise ValueError(f"Unknown reconstruction options: {sorted(unknown, key=str)}")
        finite_values(config)
        for key, value in config.items():
            if key in BOOLEAN_OPTIONS and type(value) is not bool:
                raise ValueError(f"{key} must be a JSON boolean")
            if key in INTEGER_OPTIONS:
                lower = 0 if key == "max_edits" else 1
                if type(value) is not int or value < lower:
                    raise ValueError(f"{key} must be an integer >= {lower}")
                if key == "cpus" and value > 128:
                    raise ValueError("cpus must be <= 128")
            if key in FRACTION_OPTIONS:
                if not number(value) or not 0 <= value <= 1:
                    raise ValueError(f"{key} must be a number between zero and one")
                if key == "clean_top_fraction" and value == 0:
                    raise ValueError("clean_top_fraction must be a number between zero and one")
            if key == "min_growth" and (not number(value) or value < 0):
                raise ValueError("min_growth must be a finite non-negative number")
            if key in STRING_OPTIONS and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{key} must be a nonempty string")
            if key in LIST_OPTIONS:
                if not isinstance(value, list) or any(
                    not isinstance(item, str) or not item.strip() for item in value
                ):
                    raise ValueError(f"{key} must be a list of nonempty strings")
            if key in ENUM_OPTIONS and value not in ENUM_OPTIONS[key]:
                raise ValueError(f"{key} must be one of {ENUM_OPTIONS[key]}")
            if key == "medium":
                if isinstance(value, dict):
                    if any(
                        not isinstance(k, str) or not k or not number(v) or v < 0
                        for k, v in value.items()
                    ):
                        raise ValueError(
                            "medium requires reaction IDs and non-negative finite caps"
                        )
                elif value not in ("minimal", "glucose_minimal", "rich"):
                    raise ValueError("medium must be a declared preset or uptake mapping")
        if config.get("reference_growth_ceiling") and not config.get("reference_support_path"):
            raise ValueError("reference_growth_ceiling requires reference_support_path")
        if (
            config.get("reference_growth_ceiling")
            and config.get("evaluation_track") != "reference_assisted"
        ):
            raise ValueError(
                "reference_growth_ceiling is a calibration-only reference_assisted option"
            )
        if config.get("reference_support") is False and config.get("reference_support_path"):
            raise ValueError("reference_support_path contradicts reference_support=false")
        if config.get("reference_scaffold") and not (
            config.get("reference_support") is True
            and config.get("reference_support_path")
            and config.get("evaluation_track") == "reference_assisted"
        ):
            raise ValueError(
                "reference_scaffold requires reference_assisted reference support"
            )
        if config.get("evaluation_track") in {"de_novo_public", "biomass_controlled"}:
            if config.get("reference_support", True) or config.get("reference_support_path"):
                raise ValueError(
                    "This track requires reference_support=false and no support reference"
                )
        if config.get("evaluation_track") == "reference_assisted" and not (
            config.get("reference_support") is True and config.get("reference_support_path")
        ):
            raise ValueError(
                "reference_assisted requires reference_support=true and a support reference"
            )
        if config.get("evaluation_track") == "authorized_private":
            raise ValueError(
                "authorized_private requires a verified authorization; none is configured"
            )
        object.__setattr__(self, "values", copy.deepcopy(config))

    def to_dict(self):
        return copy.deepcopy(self.values)


@dataclass(frozen=True)
class ReconstructionContext:
    forbidden_directions: tuple[tuple[str, str], ...] = ()


def quality_status_contract(report: dict[str, Any]) -> dict[str, Any]:
    """Preserve failure and unknown states; completed checks alone confer no eligibility."""
    checks = []
    for group in ("final_probes", "final_tasks"):
        for row in report.get(group, []):
            checks.append(
                {
                    **row,
                    "required": True,
                    "applicability": "applicable",
                    "scope": "strict_closure"
                    if group == "final_probes"
                    else "declared_growth_conditions",
                    "reason": row.get("detail", ""),
                }
            )
    precursor = report.get("biomass_precursors", {})
    checks.append(
        {
            "name": "biomass_precursors",
            "status": precursor.get("status", "not_run"),
            "required": True,
            "applicability": "applicable",
            "scope": "declared_medium_precursor_demands",
            "reason": "",
        }
    )
    for key, status in (
        (
            "static_balance",
            "fail"
            if report.get("static_balance", {}).get("imbalanced")
            else "incomplete"
            if report.get("static_balance", {}).get("unknown_formula_or_charge")
            else "pass"
            if "static_balance" in report
            else "not_run",
        ),
        (
            "direction",
            "fail"
            if report.get("direction", {}).get("invalid_bounds")
            else "pass"
            if "direction" in report
            else "not_run",
        ),
    ):
        checks.append(
            {
                "name": key,
                "status": status,
                "required": True,
                "applicability": "applicable",
                "scope": "model_structure",
                "reason": "",
            }
        )
    unresolved = [r["name"] for r in checks if r["status"] not in {"pass", "fail"}]
    status = (
        "incomplete"
        if unresolved
        else "passed"
        if all(r["status"] == "pass" for r in checks)
        else "failed"
    )
    return {
        "execution_status": "completed",
        "model_status": "draft_requires_independent_validation",
        "validation_status": {
            "status": status,
            "checks": checks,
            "required_checks_unresolved": len(unresolved),
        },
        "qc_complete": not unresolved,
        "incomplete_checks": unresolved,
        "benchmark_eligibility": "not_assessed",
        "repair_search_status": report.get("repair_search_status", "not_requested"),
        "final_audit_status": status,
    }
