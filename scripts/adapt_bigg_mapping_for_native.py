#!/usr/bin/env python3
"""Make an explicit native-run view without changing reference identities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from GemAgents.errors import ToolError
from GemAgents.metabolic.biomass.selection import (
    resolve_biomass_template,
    shared_prokaryotic_fallback,
)


def _catalog_path(value: Path) -> Path:
    return value / "catalog.json" if value.is_dir() else value


def _load_catalog(value: Path | None) -> tuple[dict | None, str | None]:
    if value is None:
        return None, None
    path = _catalog_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError(f"Biomass catalog cannot be read: {path}: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
        raise ToolError(f"Biomass catalog is missing a templates list: {path}")
    return payload, str(path.resolve())


def _append_reason(build: dict, reason: str) -> None:
    reasons = build.setdefault("mapping_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _block_biomass(build: dict, reason: str) -> None:
    build["status"] = "BLOCKED_MAPPING"
    build["mapping_status"] = "BLOCKED_MAPPING"
    _append_reason(build, reason)


def _resolve_native_biomass(build: dict, catalog: dict | None) -> None:
    """Resolve the source model, with the declared archaeal prokaryotic fallback."""
    requested = build.get("biomass_source_model_id")
    if not isinstance(requested, str) or not requested.strip():
        _block_biomass(build, "biomass_source_model_id_missing")
        return
    if catalog is None:
        build["biomass_template"] = requested
        build["biomass_catalog_resolution"] = {
            "status": "blocked",
            "requested": requested,
            "reason": "biomass_catalog_not_provided",
        }
        _block_biomass(build, "biomass_catalog_not_provided")
        return
    try:
        template, mode = resolve_biomass_template(
            catalog,
            requested,
            kingdom=build.get("kingdom"),
        )
    except ToolError as error:
        build["biomass_template"] = requested
        build["biomass_catalog_resolution"] = {
            "status": "blocked",
            "requested": requested,
            "reason": str(error),
        }
        _block_biomass(build, "biomass_catalog_alias_ambiguous")
        return
    if template is None and build.get("kingdom") == "archaea" and catalog is not None:
        # The source mapping may identify an archaeal BiGG model that is not
        # part of the bundled catalog.  The catalog's explicit generic
        # prokaryotic fallback is the only allowed compatibility row; retain
        # the original source model above for provenance.
        fallback = shared_prokaryotic_fallback(catalog, "archaea")
        try:
            known_template, _known_mode = resolve_biomass_template(catalog, requested)
        except ToolError:
            known_template = False
        fallback_alias = (
            fallback is not None
            and known_template is not None
            and known_template.get("id") == fallback.get("id")
        )
        if fallback is not None and (known_template is None or fallback_alias):
            build["biomass_template"] = fallback["id"]
            build["biomass_catalog_resolution"] = {
                "status": "resolved_shared_prokaryotic",
                "requested": requested,
                "selected": fallback["id"],
                "mode": "shared_prokaryotic_fallback",
                "requested_kingdom": "archaea",
                "source_kingdom": fallback.get("kingdom", "bacteria"),
                "mapping_status": fallback.get("mapping_status", "complete"),
                "reason": "no_archaeal_template_in_prokaryotic_catalog",
            }
            build[
                "native_biomass_note"
            ] = (
                "archaeal source model retained for provenance; native build uses "
                "the catalog's declared generic prokaryotic biomass equation"
            )
            return
    if template is None or (
        build.get("kingdom")
        and template.get("kingdom", "bacteria") != build.get("kingdom")
    ):
        build["biomass_template"] = requested
        build["biomass_catalog_resolution"] = {
            "status": "blocked",
            "requested": requested,
            "reason": "biomass_source_not_in_catalog_or_kingdom_mismatch",
        }
        _block_biomass(build, "biomass_source_not_in_catalog")
        return
    build["biomass_template"] = template["id"]
    build["biomass_catalog_resolution"] = {
        "status": "resolved",
        "requested": requested,
        "selected": template["id"],
        "mode": mode,
        "mapping_status": template.get("mapping_status", "complete"),
    }
    build[
        "native_biomass_note"
    ] = "declared biomass source resolved to its catalog equation; no generic fallback"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument(
        "--biomass-library",
        type=Path,
        help="catalog directory or catalog.json used to resolve declared biomass sources",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.mapping.read_text(encoding="utf-8"))
    catalog, catalog_file = _load_catalog(args.biomass_library)
    for build in payload.get("builds", []):
        if build.get("status") != "ready":
            continue
        if build.get("kingdom") not in {"bacteria", "archaea"}:
            _block_biomass(build, "native_biomass_template_unavailable_for_kingdom")
            continue
        _resolve_native_biomass(build, catalog)
    policy = payload.setdefault("policy", {})
    policy.pop("native_biomass_override", None)
    policy["native_biomass_resolution"] = {
        "catalog": catalog_file,
        "strategy": (
            "explicit biomass_source_model_id -> exact ID or declared alias; "
            "archaea may use the catalog's generic prokaryotic fallback"
        ),
        "unresolved_action": "BLOCKED_MAPPING",
        "generic_fallback": {
            "enabled": True,
            "kingdoms": ["archaea"],
            "scope": "prokaryotic",
        },
    }
    payload["counts"] = {
        **payload.get("counts", {}),
        "ready_builds": sum(row.get("status") == "ready" for row in payload["builds"]),
        "blocked_mapping_builds": sum(
            row.get("status") == "BLOCKED_MAPPING" for row in payload["builds"]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
