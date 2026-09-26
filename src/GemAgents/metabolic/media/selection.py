"""Deterministic medium preset and exchange-alias selection."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.contracts import load_configuration
from GemAgents.metabolic.media.contracts import record_applied_medium


def load_declared_medium(config: dict, default: object = "minimal") -> object:
    """Load the configured medium or a validated medium-file object."""
    medium = config.get("medium", default)
    if not config.get("medium_file"):
        return medium
    medium_path = Path(config["medium_file"])
    if not medium_path.is_file():
        raise ToolError(f"Medium file does not exist: {medium_path}")
    try:
        medium = load_configuration(medium_path)
    except (OSError, ValueError) as error:
        raise ToolError(f"Could not parse medium file: {medium_path}") from error
    if not isinstance(medium, dict):
        raise ToolError("Medium file must contain a JSON object of exchange-to-uptake rates")
    return medium


def metabolic_set_medium(model, medium: object) -> dict[str, float]:
    declared_profile = medium if isinstance(medium, str) else "custom"
    if medium == "rich":
        selected = {r.id: 10.0 for r in model.exchanges}
        # An unconstrained all-exchange medium can supply artificial energy
        # cycle substrates (for example D-alanine/alanine dipeptide).  Keep
        # the convenient rich preset, but close these known intracellular
        # energy-cycle feeds unless the user explicitly declares them.
        cycle_feed_bases = {"ala__D", "alaala"}
        for reaction in model.exchanges:
            if len(reaction.metabolites) == 1:
                metabolite = next(iter(reaction.metabolites))
                base = metabolite.id.rsplit("_", 1)[0]
                if base in cycle_feed_bases:
                    selected.pop(reaction.id, None)
    elif isinstance(medium, str) and medium in {"minimal", "glucose_minimal"}:
        # The default gap-fill environment is glucose minimal.  Resolve the
        # standard BiGG names through the same metabolite aliases used for an
        # explicit medium so ModelSEED exchanges (e.g. cpd00244/ni2) work too.
        requested = {
            "glc__D": 10.0,
            "o2": 1000.0,
            "co2": 1000.0,
            "nh4": 1000.0,
            "pi": 1000.0,
            "so4": 1000.0,
            "h2o": 1000.0,
            "h": 1000.0,
            "k": 1000.0,
            "na1": 1000.0,
            "ca2": 1000.0,
            "cl": 1000.0,
            "mg2": 1000.0,
            "mn2": 1000.0,
            "ni2": 1000.0,
            "fe2": 1000.0,
            "fe3": 1000.0,
            "zn2": 1000.0,
            "cu2": 1000.0,
            "cobalt2": 1000.0,
            "mobd": 1000.0,
            "sel": 1000.0,
            "slnt": 1000.0,
            "tungs": 1000.0,
            "cpd15574": 1000.0,
        }
        aliases = defaultdict(list)
        # Reference-template ports are intentionally separate reactions.  They
        # must not make a canonical minimal-medium alias appear ambiguous;
        # their uptake bounds are propagated from the selected native port
        # after medium resolution in the native builder.
        medium_reactions = [
            reaction for reaction in model.exchanges if "reference_boundary" not in reaction.notes
        ]
        for reaction in medium_reactions:
            aliases[reaction.id].append(reaction.id)
            if len(reaction.metabolites) == 1:
                metabolite = next(iter(reaction.metabolites))
                values = [metabolite.id, metabolite.id.rsplit("_", 1)[0]]
                for namespace in ("bigg.metabolite", "seed.compound"):
                    raw = metabolite.annotation.get(namespace, [])
                    values.extend([raw] if isinstance(raw, str) else raw)
                for alias in values:
                    if alias:
                        aliases[str(alias)].append(reaction.id)
        selected = {}
        glucose_candidates = sorted(set(aliases.get("glc__D", [])))
        for alias, rate in requested.items():
            candidates = sorted(set(aliases.get(alias, [])))
            if len(candidates) == 1:
                selected[candidates[0]] = rate
        if len(glucose_candidates) != 1:
            raise ToolError("minimal medium requires a uniquely resolved glucose exchange")
    elif isinstance(medium, dict):
        aliases = defaultdict(list)
        preferred_aliases = defaultdict(list)
        for reaction in model.exchanges:
            # Accept canonical exchange IDs as well as a metabolite ID or a
            # unique BiGG/ModelSEED metabolite alias.  Ambiguous aliases are
            # rejected instead of silently selecting one compartment.
            def add_alias(
                value: object,
                *,
                preferred: bool = False,
                reaction_id: str = reaction.id,
            ) -> None:
                if value is None:
                    return
                text = str(value).strip()
                if not text:
                    return
                aliases[text].append(reaction_id)
                aliases[text.casefold()].append(reaction_id)
                if preferred:
                    preferred_aliases[text].append(reaction_id)
                    preferred_aliases[text.casefold()].append(reaction_id)

            add_alias(reaction.id, preferred=True)
            # Older published models often omit the extracellular suffix on
            # exchange IDs (EX_cl) even though the exchanged metabolite is
            # cl_e.  Treat that spelling as an alias, not a distinct port.
            if reaction.id.startswith("EX_") and reaction.id.endswith("_e"):
                add_alias(reaction.id[:-2], preferred=True)
            if len(reaction.metabolites) == 1:
                metabolite = next(iter(reaction.metabolites))
                base = metabolite.id.rsplit("_", 1)[0]
                preferred_values = [
                    metabolite.id,
                    base,
                    f"EX_{metabolite.id}",
                    f"EX_{base}",
                ]
                # Legacy BiGG models used glc_e where current BiGG uses
                # glc__D_e.  Generate the legacy form only as an alias; the
                # ambiguity check below still rejects non-unique matches.
                legacy_base = re.sub(r"__(?:D|L)$", "", base, flags=re.IGNORECASE)
                if legacy_base != base:
                    preferred_values.extend([f"{legacy_base}_e", f"EX_{legacy_base}_e"])
                for alias in preferred_values:
                    add_alias(alias, preferred=True)
                values = []
                if metabolite.name:
                    name_alias = re.sub(r"[^A-Za-z0-9]+", "_", metabolite.name).strip("_")
                    values.extend([name_alias, f"EX_{name_alias}"])
                for namespace in ("bigg.metabolite", "seed.compound"):
                    raw = metabolite.annotation.get(namespace, [])
                    values.extend([raw] if isinstance(raw, str) else raw)
                for alias in values:
                    add_alias(alias)
        selected = {}
        unknown = []
        ambiguous = []
        exact_exchange_ids = {reaction.id for reaction in model.exchanges}
        for key, value in medium.items():
            key = str(key)
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ToolError(f"Invalid medium uptake for {key!r}") from None
            if not math.isfinite(value) or value < 0:
                raise ToolError(
                    f"Invalid medium uptake for {key!r}: expected finite nonnegative rate"
                )
            candidates = (
                [key]
                if key in exact_exchange_ids
                else sorted(
                    set(preferred_aliases.get(key, []))
                    | set(preferred_aliases.get(key.casefold(), []))
                )
            )
            if not candidates:
                candidates = sorted(
                    set(aliases.get(key, [])) | set(aliases.get(key.casefold(), []))
                )
            if not candidates:
                unknown.append(key)
                continue
            if len(candidates) != 1:
                ambiguous.append({"alias": key, "exchanges": candidates})
                continue
            canonical = candidates[0]
            if canonical in selected and not math.isclose(selected[canonical], value):
                raise ToolError(f"Medium specifies duplicate aliases for {canonical}")
            selected[canonical] = value
        if unknown or ambiguous:
            details = []
            if unknown:
                details.append(f"unknown exchanges: {sorted(unknown)}")
            if ambiguous:
                details.append(f"ambiguous metabolite aliases: {ambiguous}")
            raise ToolError("Invalid medium; " + "; ".join(details))
    else:
        raise ToolError(
            "medium must be 'minimal', 'rich', 'glucose_minimal', or an explicit "
            "exchange-to-uptake-rate dictionary"
        )
    model.medium = selected
    record_applied_medium(model, selected, str(declared_profile))
    return selected
