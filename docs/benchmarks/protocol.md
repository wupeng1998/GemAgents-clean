# Frozen benchmark protocol 1.0.0

The protocol is frozen before T15–T19 algorithm tuning. The canonical file is
`benchmarks/protocol.yaml` (JSON syntax is valid YAML 1.2), and its content hash
is stored inside the file. The empty dataset lock is intentional: this checkout
does not contain authorized measured phenotype or essentiality data, so no
biological advantage claim is made.

Public tracks are explicit:

- `de_novo_public`: no target reference support; public reaction and annotation
  priors remain declared.
- `biomass_controlled`: matched biomass is a control variable, not independent
  biomass discovery.
- `reference_assisted`: reference support is allowed and the track is explicitly
  marked not independent.

Future records require source content hashes, acquisition and authorization
metadata, sequence deduplication and lineage grouping. Missing labels remain
missing. A sample, sequence or lineage group cannot occur in both development and
test. Any endpoint change requires a new protocol version and hash; previously
exposed test data cannot be relabeled as a blind test.

The local `agent_corpus.replay_results` output is an evaluator test fixture.
Its A/B/C labels are scorer labels only, not measurements of three Agent
systems. Real route and registered-tool execution is reported from separate
scripted-model tests; online model execution and biological validation remain
separate evidence classes.
