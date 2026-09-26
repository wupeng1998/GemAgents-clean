# Capability status

The current release status is generated from accepted run manifests by
scripts/reproduce_release.py. The locked phenotype dataset is
insufficient_data, so no biological accuracy or algorithmic advantage is
claimed.

The latest engineering audit is recorded at
`artifacts/audit/round2/N17/20260921T081000Z/release-manifest.json`; it is the
reviewable manifest for the current dirty worktree. Its offline smoke passes,
while cleanup closure and package installation remain explicitly blocked. The promoted
`artifacts/releases/current.json` pointer is intentionally not advanced while
N17 remains blocked. When a release profile passes its required gates, use the
pointer and then read the versioned manifest for hashes, per-run status,
artifact availability, code identity and limitations. The root
`release-manifest.json` is retained as a historical release record.
