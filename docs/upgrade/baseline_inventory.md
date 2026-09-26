# GemAgents baseline inventory — T00, 2026-09-18

Baseline commit `55c83c8c6434e0c97b942fc928d596c9f085a9f9` on `main` matches the review package. T00 changed only audit scripts, audit tests and evidence outputs. The initial worktree contained the untracked upgrade package and ZIP. Original model artifacts were not overwritten.

[Asset lock](../../artifacts/audit/assets.lock.json): 323 entries; 321 readable files and 2 explicit blocks. File availability is separate from provenance approval. There are 143 files linked to declared provenance records, and 180 with `BLOCKED_PROVENANCE` (including excluded/unhashed files). Source statements are local records, not independent upstream verification. Content hashes identify bytes, not authorization or scientific validity.

| Asset family | Current evidence and limit |
|---|---|
| FAA/FNA/GenBank | Local public E. coli and M. genitalium inputs and BiGG FASTA files hashed. Seven extensionless zero-byte BiGG placeholders are quarantined. |
| NCBI HMM | Local enzymes.hmm, archive, index, mapping and download records hashed. Native executable availability recorded separately. |
| Reaction library | v6 manifest, catalogs, mappings, source TSVs and universes hashed; historical Windows source paths remain recorded as blocked external paths. |
| Biomass and reference models | v3 and iML1515 v12/v15 assets, public BiGG models and source declarations hashed. No public-only certification is inferred from directory names. |
| Restricted source | pear_partial_2fe2s.json recorded `BLOCKED_POLICY`; its content was not read or hashed. |
| PGAP | Deployment records and launcher available. The 81,096,867,840-byte WSL disk image was not mounted or hashed (`BLOCKED_ASSET/HASH_NOT_RUN`); its runtime contents remain unverified. Linux cannot run the stored WSL command as configured. |
| Path references | 35 present, 10 external blocked, 1 policy blocked and 1 stale Windows-relative reference (`data\\reaction_library_v6`) on Linux. |

[Historical index](../../artifacts/audit/historical-run-index.json): 180 entries, with raw models and logs, partial artifacts, summaries, text-only and config-only categories retained separately. v37 has model artifacts and a historical completed manifest; no v37 replay or final independent quality certification was performed. Subdirectory records (e.g. MEMOTE) are separate evidence entries, not additional biological replicates.

[Environment](../../artifacts/audit/T00/environment.json): Python 3.11.15 in the existing enzyagent Conda environment, installed package versions, GLPK/Scipy solver interfaces, thread settings and executable paths. The project .venv-research contains Windows Scripts/Lib and was not executed on Linux. Docker executable presence is not proof of a usable PGAP container.

[Acceptance](acceptance/T00.md): 108 existing tests plus 8 scanner regressions passed. Full Ruff produced 279 pre-existing findings (176 CarveMe, 92 Reconstructor, 11 experimental); changed audit files passed. Base Python collection failed because it lacks the project import and COBRApy; that log is preserved separately. No database download, paid LLM, PGAP rebuild, native reconstruction or MEMOTE run was performed for T00.

T00 is an inventory acceptance, not model validation. T01 can proceed with the available offline Python environment; future reconstruction gates must resolve relevant provenance, runtime and asset blocks.
