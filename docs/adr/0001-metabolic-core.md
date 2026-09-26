# ADR 0001: layered metabolic core with a compatibility facade

**Status:** accepted, 2026-09-18

## Context

`GemAgents.tools` combined the general agent dispatcher with database preparation,
annotation, reconstruction, batch jobs and CER auditing. This made scientific
code hard to review and caused optional metabolic dependencies to affect core
startup.

## Decision

New deterministic metabolic implementations belong in `GemAgents.metabolic`:

- `contracts`, `io`, `annotation`, `evidence`
- `library`, `biomass`, `media`, `reconstruction`
- `qc`, `jobs`, `pipeline`

The first mechanical migration moves the complete CER audit/repair implementation
and atomic metabolic IO. Domain facades expose the remaining legacy implementations
while later tasks move their internals by layer. `GemAgents.tools` keeps the old
imports and the `LocalToolRunner` dispatch surface. The metabolic package does not
import agent or model-provider clients and has no API-key dependency.

No modeling defaults, candidate costs, media, thresholds or solver policy change
in this migration.

## Compatibility and rollback

Existing imports such as `from GemAgents.tools import metabolic_pipeline, Auditor,
repair` remain valid. New code may import the layered modules. Rollback routes the
compatibility names to the previous implementation while retaining each mechanical
migration for independent review.
