# ADR-001: strict reconstruction configuration and separate status dimensions

Accepted for T01 on 2026-09-18. Small contract, solver-result and provenance modules
are permitted without changing the existing MQC/pear exclusions. The scientific
algorithms, evidence thresholds, media and biomass remain unchanged in this task.

`ReconstructionConfig` validates the existing configuration surface without coercion.
CLEAN predictions remain a path, and `max_edits=0` remains a valid no-edit budget.
JSON/YAML loaders reject duplicate keys (also nested and YAML merge collisions) and
nonfinite numbers. Public configuration rejects internal fields; feedback edits use
`ReconstructionContext`. The schema documents the same accepted fields.

Existing valid JSON configurations continue to work. Correct string booleans to
JSON booleans, remove duplicated keys, and use non-negative finite growth thresholds
and integer resource budgets. A reference path with `reference_support=false` is
rejected as contradictory; explicit opt-out values are preserved. The current
pipeline materializes the reference-assisted comparison defaults (strict reaction
catalog, available CLEAN predictions and public scaffold) before validation;
historical manifests remain unchanged.

Quality keeps the legacy `status` and adds execution/model/validation/benchmark
fields. Completed failed checks differ from unresolved checks. Any status other than
pass/fail remains unresolved for a required applicable check. Unspecified benchmark
eligibility stays `not_assessed`; quality checks alone cannot certify a benchmark.
Repair stop reasons remain separate from final audit outcomes. `not_applicable`
is never rewritten to pass. Static unknown chemistry is unresolved.

The repeated `boundaries` YAML mapping contained the same exclusions as the first;
remove the repeated block and retain the first block's additional `public_reuse`
policy. Git retains the original historical version; research log entries are intact.

Rollback: revert the T01 contract adapters, module, schema, tests and this instruction
update together. Existing manifests need no rewrite because legacy status is retained.
