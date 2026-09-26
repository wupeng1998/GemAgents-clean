# GemAgents 0.1.0 engineering release

This package is an engineering and quality constrained release. It does not
claim external phenotype validation or biological accuracy.

## Clean replay

1. Create the Python 3.10 CPLEX-compatible `gemagents` environment from
   environment.yml, or install the core package with
   pip install .
2. Install metabolic dependencies with
   pip install '.[metabolic,memote,benchmark,status]' when the corresponding
   assets are available.
3. Verify the package installation in an isolated temporary environment:

   conda run -n gemagents env PYTHONPATH=src python scripts/verify_package_install.py \
     --output artifacts/audit/package-installation.json

   This uses a local wheel build, a temporary venv and `--no-index --no-deps`;
   it never modifies the active environment. An incompatible interpreter is
   reported as `BLOCKED_ENVIRONMENT`.

4. Run the offline smoke and manifest generator:

   conda run -n gemagents env PYTHONPATH=src python scripts/reproduce_release.py

   The profile is explicit; the current supported profile is
   `--profile engineering`. A future quality or phenotype profile must add
   its own required checks before it can be registered.

   The default command writes a new immutable manifest under
   `artifacts/releases/0.1.0-engineering/` and atomically updates
   `artifacts/releases/current.json` only after the smoke passes. To write an
   audit attempt instead, pass explicit `--output`, `--smoke-output` and
   `--current-index` paths under that attempt directory. Pass
   `--cleanup-plan` to bind the directory cleanup report to a reviewed N12
   plan; the report is read-only and does not apply cleanup. Existing
   manifests are never overwritten.

5. Run the deterministic checks:

   conda run -n gemagents env PYTHONPATH=src python -m pytest
   conda run -n gemagents python -m ruff check src tests scripts

The generated manifest records source and code hashes, accepted run summaries,
missing large assets and known limitations. It does not copy or overwrite the
historical root `release-manifest.json` or historical run directories.

## Release gates

- engineering reproducibility: required tests and offline deterministic core;
- quality constrained: required QC and uncached certificate paths are present;
- phenotype validated: not achieved until an authorized, measured dataset is
  registered in the frozen benchmark protocol.
