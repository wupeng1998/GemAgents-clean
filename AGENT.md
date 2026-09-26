# AGENT.md

This file provides guidance to AI agents when working with this repository.

## What This Project Is

GemAgents is a pure Python terminal coding agent built on LangGraph. Its
deterministic workflows and agent tools are implemented as Python-native modules.

- Runtime: Python 3.10 (CPLEX-compatible primary profile); Python 3.11 remains supported by the package range
- Environment: conda environment named `gemagents`
- Package layout: `src/GemAgents`
- Tests: `pytest`
- Quality gate: `ruff`

## Commands

```bash
conda env create -f environment.yml
conda activate gemagents
conda run -n gemagents env PYTHONPATH=src python -m pytest
conda run -n gemagents python -m ruff check src tests scripts
```

Run the CLI:

```bash
gemagents -p "Inspect this project."
gemagents
```

## Boundaries

Keep changes focused on the Python package, tests, and documentation. Do not
reintroduce npm, TypeScript, React Ink, or a parallel JavaScript runtime unless
the project direction explicitly changes.

Add metabolic behavior under `src/GemAgents/metabolic/` at the matching layer.
`GemAgents.tools` remains the compatibility facade and local tool dispatcher;
do not add new metabolic implementations to that monolith. Keep other behavior
inside the existing Python feature modules.

## Metabolic reconstruction boundaries

Use the public CarveMe, Reconstructor, NCBI, ModelSEED,
COBRApy and MEMOTE resources and independently written code.
Keep provider credentials in environment variables, never project files or reports.
For reconstruction use the deterministic CLI or `metabolic_start` / `metabolic_status`.
Do not use LLM output as sequence annotation, GPR evidence, or a quality certificate.
Full PGAP, NCBI HMM enzyme annotation, and imported GenBank annotation are distinct
routes. Report the actual route and unresolved checks. Do not silently substitute
a gene caller for PGAP. See `docs/genome-reconstruction.zh-CN.md` for runnable usage.

## Upgrade implementation modules

The deterministic reconstruction is split under `src/GemAgents/metabolic/` by
contract, IO, annotation, evidence, library, biomass, media, reconstruction, QC,
jobs and pipeline layers. Keep the source exclusions above. User configuration
and internal retry context must remain separate. See
`docs/upgrade/ADR-001-contracts.md` and `docs/adr/0001-metabolic-core.md`.

## Release gate

Use docs/releases/REPRODUCE.md and release-manifest.json for the current
engineering release status. Do not describe the insufficient phenotype dataset
as external biological validation; preserve BLOCKED asset and failed-run records.
