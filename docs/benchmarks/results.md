# Benchmark results

The benchmark protocol is frozen at version 1.0.0. The dataset lock currently
reports insufficient_data, so this repository does not claim biological
accuracy or an algorithmic advantage.

Benchmark outputs must separate:

- engineering execution and failure counts;
- semantic consistency and independent certificate results;
- phenotype metrics with unknown labels and execution failures retained;
- solver and wall-clock cost.

All comparisons require the same sample, medium hash, biomass identifier,
evaluation track and protocol hash. Reference-assisted overlap is excluded from
de novo accuracy. Any future measured table must include the full failure
denominator and clustered uncertainty by strain.

Synthetic `agent_corpus.replay_results` output is an evaluator test fixture,
not A/B/C system performance. It is kept for scorer and leakage regression
checks only. Scripted-model route/tool execution, online model execution and
biological validation are recorded separately.
