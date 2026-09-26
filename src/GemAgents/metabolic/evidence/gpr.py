"""Conservative reference GPR compilation with relation-level evidence."""

from __future__ import annotations

import ast
import hashlib


def _identity(query: str, reference: str) -> tuple[float, float]:
    if not query or not reference:
        return 0.0, 0.0
    length = min(len(query), len(reference))
    matches = sum(a == b for a, b in zip(query[:length], reference[:length], strict=True))
    return matches / length, length / len(reference)


def compile_reference_gpr(
    rule: str,
    reference_genes: dict,
    evidence: list[dict],
    proteins: dict[str, str],
    *,
    min_identity: float,
    min_coverage: float,
    policy: str = "legacy_v1",
) -> tuple[str, dict]:
    if policy not in {"legacy_v1", "structured_v2"}:
        raise ValueError("Unknown evidence_policy")
    details = {
        "compiler_version": 2,
        "evidence_policy": policy,
        "reference_rule": rule,
        "matched": {},
        "relations": {},
        "unmatched": [],
        "unresolved_complexes": [],
    }

    evidence_rows = [
        row
        for row in evidence
        if str(row.get("source", "")).casefold() != "gapfill_hypothesis"
    ]
    sequence_hashes = {
        str(row.get("input_gene_id") or row.get("gene_id") or ""): hashlib.sha256(
            str(proteins.get(str(row.get("input_gene_id") or row.get("gene_id") or ""), ""))
            .upper()
            .removesuffix("*")
            .encode()
        ).hexdigest()
        for row in evidence_rows
        if proteins.get(str(row.get("input_gene_id") or row.get("gene_id") or ""))
    }

    def candidates(gene: str) -> list[str]:
        reference = reference_genes.get(gene, {})
        values = []
        relations = []
        for row in evidence_rows:
            input_gene = str(row.get("input_gene_id") or row.get("gene_id") or "")
            sequence = proteins.get(input_gene, "")
            identity = coverage = 0.0
            match_route = None
            if reference.get("sequence_sha256") and sequence:
                if sequence_hashes.get(input_gene) == reference["sequence_sha256"]:
                    identity, coverage, match_route = 1.0, 1.0, "exact_sequence_hash"
            if match_route is None:
                symbol = str(reference.get("gene_symbol", "")).casefold()
                row_symbols = {
                    str(value).casefold()
                    for value in (row.get("gene_symbol", ""), row.get("reference_id", ""))
                    if value
                }
                same_function = bool(set(reference.get("ec", [])) & set(row.get("ec", [])))
                if symbol and symbol in row_symbols and same_function:
                    identity, coverage = _identity(sequence, str(reference.get("sequence", "")))
                    if identity >= min_identity and coverage >= min_coverage:
                        match_route = "identity_and_coverage"
            if match_route:
                values.append(row)
                relations.append(
                    {
                        "input_gene_id": input_gene,
                        "sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                        "annotation_route": row.get("source", "unresolved"),
                        "source_class": "reference_protein_match",
                        "reference_match_route": match_route,
                        "database_version": row.get("database_version", "unresolved"),
                        "model_accession": row.get("accession"),
                        "thresholds": {
                            "min_identity": min_identity,
                            "min_coverage": min_coverage,
                        },
                        "identity": identity,
                        "coverage": coverage,
                        "ambiguity": sorted(set(row.get("ec", []))),
                        "complex_status": row.get("complex_status", "unresolved"),
                        "compartment_support": row.get("compartment_support", "unresolved"),
                    }
                )
        unique = {row["gene_id"]: row for row in values}
        details["matched"][gene] = sorted(unique)
        details["relations"][gene] = relations
        if not unique:
            details["unmatched"].append(gene)
        return sorted(unique)

    try:
        tree = ast.parse(rule, mode="eval").body
    except SyntaxError:
        details["rejection_reason"] = "invalid_reference_rule"
        return "", details

    def render(node) -> str:
        if isinstance(node, ast.Name):
            return " or ".join(candidates(node.id))
        if isinstance(node, ast.BoolOp):
            joiner = " and " if isinstance(node.op, ast.And) else " or "
            children = [render(item) for item in node.values]
            if joiner == " and " and any(not child for child in children):
                details["unresolved_complexes"].append(ast.unparse(node))
                return ""
            children = [child for child in children if child]
            return joiner.join(
                f"({child})" if joiner == " and " and " or " in child else child
                for child in children
            )
        return ""

    compiled = render(tree)
    if not compiled:
        details["rejection_reason"] = "incomplete_or_unsupported_reference_rule"
    return compiled, details
