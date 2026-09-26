# GemAgents environment matrix

## Package identity

The published distribution and primary command are `gemagents`. Python imports
are provided by `GemAgents`.

## Dependency profiles

| Profile | Install | Purpose |
|---|---|---|
| core | `pip install .` | Agent, CLI, sessions and nonmetabolic tools |
| metabolic | `pip install '.[metabolic]'` | COBRA/GLPK, HMM annotation and native reconstruction |
| benchmark | `pip install '.[benchmark]'` | Workbook based public benchmark preparation |
| status | `pip install '.[status]'` | Process identity checks for background job status |
| MEMOTE | `pip install '.[metabolic,memote]'` | Isolated MEMOTE worker and raw reports |
| development | `pip install '.[dev,metabolic,benchmark,status]'` | Tests, lint and wheel builds |
| PGAP runtime | See `docs/pgap-deployment.zh-CN.md` | Separate WSL/container runtime; no Python extra installs PGAP |

## Verified matrix

| Component | Verified value | Evidence |
|---|---|---|
| Python | CPython 3.10.x primary (`gemagents`, CPLEX), 3.11.x compatibility | local CPLEX probe and package compatibility contract |
| COBRApy | 0.32.1 | `gemagents` Python 3.10 validation environment |
| optlang / GLPK | optlang 1.9.1 / swiglpk 5.0.13 | lightweight solver and SBML tests |
| MEMOTE | 0.17.0, isolated subprocess, one process | MEMOTE failure/status parsing tests |
| Python range | `>=3.10,<3.12` | 3.10 is the primary CPLEX profile; 3.11 remains supported |

`uv.lock` is the resolver lock for all declared extras. Install the exact locked
environment with `uv sync --all-extras --frozen`. `environment.yml` creates the
full Conda profile under the `gemagents` environment name and installs the pinned
CPLEX 22.1.1.0 solver profile. CPLEX is kept outside the publishable wheel
dependencies because it is a licensed, platform-specific solver.

CI uses the committed Python 3.10 profile locks (`constraints/py310-dev.lock`,
`constraints/py310-dev-metabolic.lock` and
`constraints/py310-dev-metabolic-memote.lock`). Each lane installs its hashed
requirements with `pip install --require-hashes -r` and then installs the local
project with `--no-deps --no-build-isolation`; passing a local directory through
`-c` is invalid because pip cannot hash that directory.

The container recipe currently pins the historical official `python:3.11.15-slim` multi-platform index
to `sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93`.
Its registry evidence is recorded in `containers/base-image.json`; this host has
the Docker client but no daemon, so no local image result is claimed.

The CI core job installs no metabolic extra. The metabolic fixture job exercises
GLPK and SBML round trips. MEMOTE runs in a manually requested profile and keeps
raw output; it does not modify upstream tests or scores. Full PGAP and datasets
requiring authorization remain manual integration work.

## Upgrade

Create a new environment instead of renaming or updating an unrelated existing
environment:

```bash
conda env create -f environment.yml
conda activate gemagents
gemagents --help
```

Historical audit artifacts retain the names that were recorded when they were
created. All new commands and replayed checks use the `gemagents` environment
and `gemagents` CLI. Reconstruction defaults are defined by the current
`ReconstructionConfig` contract; solver thresholds, media and biomass behavior
remain explicit configuration choices.
