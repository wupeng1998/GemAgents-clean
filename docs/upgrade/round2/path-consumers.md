# Round 2 path-consumer inventory

This additive map records current path boundaries. It does not authorize moving
historical files or assets.

| Consumer | Canonical resolver | Evidence | Current boundary |
| --- | --- | --- | --- |
| CLI workspace, settings, event log | `resolve_workspace()` + `RepoLayout` | `tests/test_cli_builtins.py`, `tests/test_extensions_settings_session.py`, `tests/test_logging_policy.py` | bounded |
| Project context, sessions, file history, extensions | `RepoLayout` / `UserLayout` | `tests/test_lifecycle_context.py`, `tests/test_extensions_settings_session.py` | bounded; user root is read-only |
| Contracts, reconstruction inputs and run outputs | `RepoLayout.resolve()` / `writable()` and `compile_contract()` | `tests/test_reconstruction_routing.py`, `tests/test_metabolic_contracts.py` | bounded |
| MEMOTE model input | `RepoLayout.resolve()` | `tests/test_cli_builtins.py` | bounded |
| Reaction-library BiGG input | `RepoLayout.resolve()` | `tests/test_cli_builtins.py` | bounded |
| Biomass spec/library inputs | `RepoLayout.resolve()` | `tests/test_cli_builtins.py` | bounded |
| Explicit reconstruction config | `RepoLayout.resolve()` | `tests/test_cli_builtins.py` | bounded |
| Jobs, checkpoints and recovery artifacts | `RepoLayout.jobs` plus workspace-relative checks | `tests/test_job_lifecycle.py`, `tests/test_pipeline_resume.py` | bounded |
| Versioned assets and historical aliases | `ArtifactLocator` / `resolve_artifact()` | `tests/test_asset_locator.py`, `tests/test_relocation.py` | hash-checked, read-only |
| Historical index and repository inventory | explicit `--root`, bounded output | `tests/test_historical_run_index.py`, `tests/test_round2_inventory.py` | dry-run |
| Cleanup/archive review | `RepoLayout` + symlink-component checks | `tests/test_maintain_repository.py`, `tests/test_logging_policy.py` | plan/validate only; no apply/purge |
| Third-party and large-asset provenance | metadata-only `asset_provenance.py` | `tests/test_asset_provenance.py` | KEEP / approval required |

Intentional compatibility boundaries:

- `resolve_workspace(None)` still uses caller cwd when no workspace is supplied;
  explicit workspace callers do not inherit cwd-relative settings or outputs.
- `UserLayout` exposes `~/.gemagents` as the read-only user configuration root;
  repository runtime state is stored under `.gemagents/`.
- Historical audit artifacts remain byte-for-byte unchanged. Locator and
  relocation indexes provide aliases without changing their source bytes.

The active runtime migration is complete; historical audit paths remain preserved
for provenance.
