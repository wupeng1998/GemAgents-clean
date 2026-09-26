# GemAgents repository instructions

Read `AGENT.md` for the existing project boundaries and commands.  Credentials belong in environment variables.

Place deterministic metabolic implementations in the matching
`src/GemAgents/metabolic/` layer; `GemAgents.tools` is a compatibility facade and
dispatcher (ADR-001 and `docs/adr/0001-metabolic-core.md`). Keep modeling defaults,
source policy and numerical thresholds unchanged unless the current task specifies
a change. Validate user inputs before starting a reconstruction and keep internal
retry context separate. Preserve historical runs and raw evidence.

Use the project `gemagents` environment for checks. From this workspace, run
`conda run -n gemagents env PYTHONPATH=src python -m pytest` and
`conda run -n gemagents python -m ruff check src tests scripts`. Existing experimental/vendor Ruff findings
are documented under `artifacts/audit/T00`; do not suppress them to claim success.

Release claims must be sourced from release-manifest.json and
docs/releases/REPRODUCE.md. The locked phenotype dataset is insufficient for
biological advantage claims, and missing assets or failed runs must remain
explicit in generated status tables.
