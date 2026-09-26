"""Biomass template ranking helpers with deterministic sequence sketches."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.sequence import metabolic_detect_input, metabolic_fasta


def sequence_sketch(records, alphabet: str, k: int = 21, size: int = 2048) -> list[int]:
    """Deterministic bottom-k sketch used only to rank public templates."""
    values = set()
    for _, sequence in records:
        sequence = sequence.upper()
        for i in range(max(0, len(sequence) - k + 1)):
            kmer = sequence[i : i + k]
            if alphabet == "dna":
                reverse = str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN")
                kmer = min(kmer, kmer.translate(reverse)[::-1])
            digest = hashlib.blake2b(kmer.encode(), digest_size=8).digest()
            values.add(int.from_bytes(digest, "big"))
    return sorted(values)[:size]

def sketch_similarity(left: list[int], right: list[int]) -> float:
    if not left or not right:
        return 0.0
    a, b = set(left), set(right)
    return len(a & b) / len(a | b)



def _biomass_reaction_is_eligible(template: dict) -> bool:
    """Return whether a catalog row contains a real biomass objective."""
    reaction = template.get("biomass_reaction")
    if not isinstance(reaction, dict):
        return False
    stoichiometry = reaction.get("stoichiometry")
    if not isinstance(stoichiometry, dict) or not stoichiometry:
        return False
    reaction_id = str(reaction.get("id", "")).strip().upper()
    reaction_name = str(reaction.get("name", "")).casefold()
    if reaction_id.split("_", 1)[0] in {"EX", "DM", "SK"}:
        return False
    if "biomass" not in reaction_id.casefold() and "biomass" not in reaction_name:
        if any(word in reaction_name.split() for word in ("exchange", "demand", "sink")):
            return False
    return True


def _catalog_aliases(template: dict) -> set[str]:
    """Collect source identifiers accepted by explicit biomass resolution."""
    values: set[str] = set()
    for key in (
        "id",
        "source_model_id",
        "source_model",
        "source_id",
        "biomass_source_id",
        "model",
    ):
        value = template.get(key)
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    reaction = template.get("biomass_reaction")
    if isinstance(reaction, dict):
        value = reaction.get("id")
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    for key in ("selection_aliases", "aliases", "source_aliases"):
        aliases = template.get(key, ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        if isinstance(aliases, (list, tuple, set)):
            values.update(str(value).strip() for value in aliases if str(value).strip())
    return values


def _normalise_catalog_alias(value: str) -> str:
    """Normalise POSIX/Windows model paths while preserving model IDs."""
    value = value.strip().replace("\\", "/")
    while value.lower().endswith((".gz", ".json", ".xml")):
        value = value.rsplit(".", 1)[0]
    return value.rsplit("/", 1)[-1].casefold()


def shared_prokaryotic_fallback(catalog: dict, requested_kingdom: str) -> dict | None:
    """Return the declared generic template for archaeal prokaryotic runs.

    The bundled catalog is intentionally a *prokaryotic* catalog, although its
    source rows predate the shared bacteria/archaea policy and carry
    ``kingdom=bacteria``.  Only its declared ``fallback_template`` is eligible
    for cross-domain reuse; species-specific bacterial equations must not be
    silently applied to archaea.
    """
    if requested_kingdom != "archaea":
        return None
    if not (
        catalog.get("taxonomy_policy") == "shared_prokaryotic_biomass"
        or "prokaryotic" in str(catalog.get("scope", "")).casefold()
    ):
        return None
    policy = catalog.get("diamond_selection", {})
    fallback_id = policy.get("fallback_template")
    if not isinstance(fallback_id, str) or not fallback_id.strip():
        return None
    for template in catalog.get("templates", []):
        applicable = template.get("applicable_kingdoms") if isinstance(template, dict) else None
        if (
            isinstance(template, dict)
            and template.get("id") == fallback_id
            and template.get("kingdom", "bacteria") == "bacteria"
            and (not isinstance(applicable, list) or "archaea" in applicable)
            and _biomass_reaction_is_eligible(template)
        ):
            return template
    return None


def _shared_prokaryotic_report(
    template: dict,
    *,
    requested_kingdom: str,
    requested_template: str | None,
    mode: str,
) -> dict:
    """Describe a generic prokaryotic equation reused for an archaeal run."""
    return {
        "selected_template": template["id"],
        "selected_similarity": None,
        "selection_mode": mode,
        "requested_kingdom": requested_kingdom,
        "source_kingdom": template.get("kingdom", "bacteria"),
        "taxonomy_policy": "shared_prokaryotic_biomass",
        "requested_template": requested_template,
        "fallback_reason": "no_archaeal_template_in_prokaryotic_catalog",
        "mapping_status": template.get(
            "mapping_status", "complete" if template.get("usable", True) else "partial"
        ),
        "ranked_templates": [],
    }


def _diamond_selection(
    catalog: dict,
    input_path: Path,
    kind: str,
    config: dict,
    templates: list[dict],
) -> tuple[dict, dict] | None:
    """Select a biomass template from the catalog's declared DIAMOND policy."""
    if catalog.get("selection_method") != "diamond_hit_count":
        return None
    policy = catalog.get("diamond_selection", {})
    references = policy.get("references", [])
    fallback_id = policy.get("fallback_template")
    by_id = {str(template.get("id")): template for template in templates}
    if not references:
        fallback = by_id.get(str(fallback_id))
        if fallback is None or not _biomass_reaction_is_eligible(fallback):
            return None
        return fallback, {
            "query_kind": kind,
            "selected_template": fallback["id"],
            "selected_similarity": None,
            "selection_mode": "general_biomass_fallback",
            "ranked_templates": [],
        }

    library = Path(str(config.get("biomass_library", "")))
    executable = (
        Path(str(config["biomass_diamond"]))
        if config.get("biomass_diamond")
        else library / str(policy.get("diamond_executable", "bin/diamond"))
    )
    if not executable.is_file():
        executable = Path(shutil.which("diamond") or "")
    if not executable.is_file():
        raise ToolError(f"Biomass DIAMOND executable is missing: {executable}")
    identity_threshold = float(policy.get("identity_threshold", 90.0))
    e_value_threshold = float(policy.get("e_value_threshold", 1e-10))
    command_name = "blastx" if kind == "fna" else "blastp"
    ranked = []
    for order, reference in enumerate(references):
        template_id = str(reference.get("template", ""))
        template = by_id.get(template_id)
        if template is None or template.get("kingdom", "bacteria") != config.get(
            "kingdom", "bacteria"
        ):
            continue
        database = Path(str(reference.get("diamond_database", "")))
        if not database.is_absolute():
            database = library / database
        result = subprocess.run(
            [
                str(executable),
                command_name,
                "--db",
                str(database),
                "--query",
                str(input_path),
                "--outfmt",
                "6",
                "qseqid",
                "sseqid",
                "pident",
                "length",
                "mismatch",
                "gapopen",
                "qstart",
                "qend",
                "sstart",
                "send",
                "evalue",
                "bitscore",
                "--evalue",
                str(e_value_threshold),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ToolError(
                f"Biomass DIAMOND selection failed for {template_id}: {result.stderr.strip()}"
            )
        hits = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 11:
                continue
            try:
                identity = float(fields[2])
                e_value = float(fields[10])
            except ValueError:
                continue
            if identity >= identity_threshold and e_value <= e_value_threshold:
                hits.append((fields[0], fields[1]))
        ranked.append(
            {
                "template": template_id,
                "qualifying_hits": len(hits),
                "identity_threshold": identity_threshold,
                "e_value_threshold": e_value_threshold,
                "order": order,
            }
        )
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row["qualifying_hits"], row["order"]))
    selected = by_id[ranked[0]["template"]]
    for row in ranked:
        row.pop("order", None)
    return selected, {
        "query_kind": kind,
        "selected_template": selected["id"],
        "selected_similarity": None,
        "selection_mode": "diamond_hit_count",
        "identity_threshold": identity_threshold,
        "e_value_threshold": e_value_threshold,
        "ranked_templates": ranked,
    }


def resolve_biomass_template(
    catalog: dict,
    requested: str,
    *,
    kingdom: str | None = None,
) -> tuple[dict | None, str | None]:
    """Resolve an explicit source model/template without a generic fallback."""
    if not isinstance(requested, str) or not requested.strip():
        return None, None
    templates = catalog.get("templates", []) if isinstance(catalog, dict) else []
    if not isinstance(templates, list):
        return None, None
    requested = requested.strip()
    exact = [
        template
        for template in templates
        if isinstance(template, dict) and template.get("id") == requested
    ]
    candidates = exact or [
        template
        for template in templates
        if isinstance(template, dict)
        and any(
            _normalise_catalog_alias(requested) == _normalise_catalog_alias(alias)
            for alias in _catalog_aliases(template)
        )
    ]
    if kingdom is not None:
        candidates = [
            template
            for template in candidates
            if template.get("kingdom", "bacteria") == kingdom
        ]
    if len(candidates) > 1:
        ids = ", ".join(sorted(str(template.get("id", "")) for template in candidates))
        raise ToolError(f"biomass source alias maps ambiguously to: {ids}")
    if len(candidates) != 1:
        return None, None
    template = candidates[0]
    if not _biomass_reaction_is_eligible(template):
        return None, None
    mode = "explicit_template" if exact else "explicit_catalog_alias"
    return template, mode

def choose_biomass(
    catalog: dict, input_path: Path, kind: str, config: dict
) -> tuple[dict, dict]:
    if kind == "auto":
        kind = metabolic_detect_input(input_path, "auto")[0]
    requested = config.get("biomass_template")
    if requested:
        requested_kingdom = config.get("kingdom", "bacteria")
        template, mode = resolve_biomass_template(
            catalog,
            str(requested),
            kingdom=requested_kingdom,
        )
        if template is None:
            # Archaeal inputs reuse only the catalog's declared generic
            # prokaryotic equation.  Keep the requested source model in the
            # report so this compatibility policy cannot be mistaken for an
            # archaeal-specific source match.
            shared = shared_prokaryotic_fallback(catalog, requested_kingdom)
            requested_alias = str(requested)
            known_requested = any(
                isinstance(item, dict)
                and (
                    item.get("id") == requested_alias
                    or any(
                        _normalise_catalog_alias(requested_alias)
                        == _normalise_catalog_alias(alias)
                        for alias in _catalog_aliases(item)
                    )
                )
                for item in catalog.get("templates", [])
            )
            shared_alias = shared is not None and (
                shared.get("id") == requested_alias
                or any(
                    _normalise_catalog_alias(requested_alias)
                    == _normalise_catalog_alias(alias)
                    for alias in _catalog_aliases(shared)
                )
            )
            if shared is not None and (not known_requested or shared_alias):
                return shared, {
                    "query_kind": kind,
                    **_shared_prokaryotic_report(
                        shared,
                        requested_kingdom=requested_kingdom,
                        requested_template=str(requested),
                        mode="shared_prokaryotic_fallback",
                    ),
                }
        if template is None:
            # Distinguish a real missing/invalid template from a template that
            # exists but belongs to another taxonomic domain.  The latter was
            # previously reported as "not available", obscuring why the
            # archaeal BiGG row could not use the bacterial ``tongyong`` row.
            requested_alias = str(requested)
            taxonomy_matches = [
                item
                for item in catalog.get("templates", [])
                if isinstance(item, dict)
                and (
                    item.get("id") == requested_alias
                    or any(
                        _normalise_catalog_alias(requested_alias)
                        == _normalise_catalog_alias(alias)
                        for alias in _catalog_aliases(item)
                    )
                )
            ]
            mismatched_kingdoms = sorted(
                {
                    str(item.get("kingdom", "bacteria"))
                    for item in taxonomy_matches
                    if item.get("kingdom", "bacteria") != requested_kingdom
                }
            )
            if mismatched_kingdoms and not any(
                item.get("kingdom", "bacteria") == requested_kingdom
                for item in taxonomy_matches
            ):
                raise ToolError(
                    f"Requested biomass_template {requested!r} belongs to kingdom "
                    f"{', '.join(mismatched_kingdoms)}; requested kingdom is "
                    f"{requested_kingdom!r}"
                )
            available = ", ".join(
                sorted(
                    str(item.get("id", ""))
                    for item in catalog.get("templates", [])
                    if isinstance(item, dict) and item.get("id")
                )
            )
            raise ToolError(
                f"Requested biomass_template {requested!r} is not available in the catalog; "
                f"catalog ids: {available or '<none>'}"
            )
        # An explicit template is a user-declared objective, not a taxonomy
        # inference. It must remain usable without reference proteomes; this
        # also lets blind benchmarks borrow only the published biomass
        # equation without exposing reference GPRs to reconstruction.
        return template, {
            "query_kind": kind,
            "selected_template": template["id"],
            "selected_similarity": None,
            "selection_mode": mode,
            "mapping_status": template.get(
                "mapping_status", "complete" if template.get("usable", True) else "partial"
            ),
            "ranked_templates": [],
        }
    else:
        shared = shared_prokaryotic_fallback(catalog, config.get("kingdom", "bacteria"))
        if shared is not None:
            return shared, {
                "query_kind": kind,
                **_shared_prokaryotic_report(
                    shared,
                    requested_kingdom=config.get("kingdom", "bacteria"),
                    requested_template=None,
                    mode="shared_prokaryotic_fallback",
                ),
            }
        all_templates = list(catalog.get("templates", []))
        templates = (
            all_templates
            if catalog.get("selection_method") == "diamond_hit_count"
            else [
                t
                for t in all_templates
                if t.get("usable") or t.get("selection_eligible", False)
            ]
        )
        if not templates and all_templates:
            # Keep the rows long enough to report a useful sequence-length
            # error below; eligibility is checked when a candidate is ranked.
            templates = all_templates
        if not templates:
            raise ToolError("Biomass catalog has no usable templates")
    diamond = _diamond_selection(catalog, input_path, kind, config, templates)
    if diamond is not None:
        return diamond
    records = metabolic_fasta(input_path, kind)
    query_sketch = sequence_sketch(
        records, "dna" if kind == "fna" else "protein", k=21 if kind == "fna" else 7
    )
    if not query_sketch:
        raise ToolError("Input sequence is too short for biomass selection")
    ranked = []
    for template in templates:
        if template.get("kingdom", "bacteria") != config.get("kingdom", "bacteria"):
            continue
        template_sketch = (
            template.get("genome_sketch", [])
            if kind == "fna"
            else template.get("protein_sketch", [])
        )
        if not template_sketch:
            continue
        ranked.append(
            {
                "template": template["id"],
                "similarity": sketch_similarity(query_sketch, template_sketch),
            }
        )
    if not ranked:
        raise ToolError("No biomass template matches the input type and kingdom")
    ranked.sort(key=lambda row: (-row["similarity"], row["template"]))
    best = ranked[0]
    if best["similarity"] < float(config.get("biomass_min_similarity", 0.0)):
        raise ToolError("No biomass template reaches biomass_min_similarity")
    return next(t for t in templates if t["id"] == best["template"]), {
        "query_kind": kind,
        "selected_template": best["template"],
        "selected_similarity": best["similarity"],
        "selection_mode": "auto_sequence_sketch",
        "mapping_status": next(
            t.get("mapping_status", "complete" if t.get("usable", True) else "partial")
            for t in templates
            if t["id"] == best["template"]
        ),
        "ranked_templates": ranked,
        "similarity_method": "bottom-k blake2b sketch; ranking only, not ANI",
        "partial_support": next(
            t.get("partial_support", {}) for t in templates if t["id"] == best["template"]
        ),
    }
