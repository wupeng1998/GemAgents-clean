"""Reference-support helpers used by native reconstruction.

The public iML1515 JSON stores gene identifiers and annotations, but no amino
acid sequences.  Reference GPRs therefore used to be discarded wholesale by
the evidence compiler: the biomass catalog's ``proteins`` list is intentionally
empty for this model.  ``load_reference_genes`` restores the missing bridge by
joining the public model's ``b`` locus tags to the deterministic MG1655 protein
FASTA and recording sequence hashes. Unique exact matches can support transfer
of reference GPRs; they are recorded separately from NCBI and CLEAN annotations.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from GemAgents.metabolic.evidence.gpr import compile_reference_gpr


def _fasta_sequences(path: Path) -> dict[str, str]:
    """Read a small reference protein FASTA without changing query parsing."""
    if not path.is_file():
        return {}
    sequences: dict[str, str] = {}
    identifier = None
    chunks: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if identifier is not None:
                    sequences[identifier] = "".join(chunks).upper()
                identifier = line[1:].split(None, 1)[0]
                chunks = []
            elif identifier is not None:
                chunks.append(line)
    if identifier is not None:
        sequences[identifier] = "".join(chunks).upper()
    return sequences


def load_reference_genes(
    source_path: Path,
    *,
    reference_fasta: str | Path | None = None,
) -> dict[str, dict]:
    """Load public-reference genes and sequence hashes for GPR compilation.

    ``source_path`` may be a COBRA JSON model (optionally gzipped) or SBML.  A
    caller can supply an explicit reference FASTA; when omitted, the bundled
    CarveMe MG1655 benchmark FASTA is used when present.  Missing assets are
    represented as an empty result and leave the conservative historical
    behavior intact.
    """
    if not source_path.is_file():
        return {}
    try:
        source_name = source_path.name.casefold()
        if source_name.endswith(".json.gz"):
            from cobra.io import model_from_dict

            with gzip.open(source_path, "rt", encoding="utf-8") as handle:
                source = model_from_dict(json.load(handle))
        elif source_name.endswith(".json"):
            from cobra.io import load_json_model

            source = load_json_model(str(source_path))
        elif source_name.endswith((".sbml", ".xml", ".sbml.gz", ".xml.gz")):
            from cobra.io import read_sbml_model

            source = read_sbml_model(str(source_path))
        else:
            return {}
    except (OSError, ValueError, TypeError, KeyError):
        return {}

    fasta_path = Path(reference_fasta).expanduser() if reference_fasta else None
    if fasta_path is not None and not fasta_path.is_absolute():
        fasta_path = (source_path.parent / fasta_path).resolve()
    if fasta_path is None or not fasta_path.is_file():
        # Resolve relative to the repository when this package is imported from
        # an editable checkout; an unavailable optional asset simply yields no
        # sequence-backed reference GPRs.
        repository_root = Path(__file__).resolve().parents[4]
        bundled = repository_root / "carveme/carveme/data/benchmark/fasta/Ecoli_K12_MG1655.faa"
        fasta_path = bundled if bundled.is_file() else None
    sequences = _fasta_sequences(fasta_path) if fasta_path else {}
    hashes = {
        identifier: hashlib.sha256(sequence.encode()).hexdigest()
        for identifier, sequence in sequences.items()
    }
    reference_genes: dict[str, dict] = {}
    for gene in source.genes:
        annotation = dict(gene.annotation or {})
        locus_values = annotation.get("refseq_locus_tag", [])
        locus = locus_values[0] if isinstance(locus_values, list) and locus_values else locus_values
        sequence_hash = hashes.get(gene.id) or (hashes.get(str(locus)) if locus else None)
        entry = {
            "gene_symbol": gene.name or annotation.get("refseq_name", "") or gene.id,
            "sequence_sha256": sequence_hash,
            "reference_id": gene.id,
            "db_xref": [f"GeneID:{value}" for value in annotation.get("ncbigene", [])]
            if isinstance(annotation.get("ncbigene", []), list)
            else [],
        }
        reference_genes[gene.id] = entry
    return reference_genes


def sequence_identity(query: str, reference: str) -> tuple[float, float]:
    """Return positional identity and reference coverage for two sequences."""
    if not query or not reference:
        return 0.0, 0.0
    length = min(len(query), len(reference))
    identity = sum(
        left == right
        for left, right in zip(query[:length], reference[:length], strict=True)
    ) / max(length, 1)
    return identity, length / max(len(reference), 1)


def compile_gpr(
    rule: str,
    reference_genes: dict,
    evidence: list[dict],
    proteins: dict,
    config: dict,
) -> tuple[str, dict]:
    """Compile a public-reference GPR under the configured evidence policy."""
    return compile_reference_gpr(
        rule,
        reference_genes,
        evidence,
        proteins,
        min_identity=float(config.get("gpr_min_identity", 0.7)),
        min_coverage=float(config.get("gpr_min_coverage", 0.7)),
        policy=str(config.get("evidence_policy", "legacy_v1")),
    )


def apply_template_gprs(
    universal,
    template: dict,
    support_aliases: list[dict],
    reference_genes: dict,
    evidence: list[dict],
    proteins: dict,
    config: dict,
    mapped: dict,
) -> dict:
    """Compile template and alias GPRs into the mapped reaction evidence."""
    rule_details = {}
    for row in template.get("gpr_templates", []):
        compiled, details = compile_gpr(
            row["gene_reaction_rule"],
            reference_genes,
            evidence,
            proteins,
            config,
        )
        rule_details[row["reaction_id"]] = details
        if compiled and row["reaction_id"] in universal.reactions:
            mapped.setdefault(
                row["reaction_id"],
                {
                    "genes": [],
                    "gpr_genes": [],
                    "ec": [],
                    "sources": [],
                    "score": 1.0,
                    "ambiguous": False,
                },
            )["gpr_rule"] = compiled
    for alias in support_aliases:
        reaction_id = alias.get("canonical_reaction_id")
        source_rule = alias.get("gene_reaction_rule", "")
        if not reaction_id or not source_rule or reaction_id not in universal.reactions:
            continue
        compiled, details = compile_gpr(
            source_rule,
            reference_genes,
            evidence,
            proteins,
            config,
        )
        rule_details[reaction_id] = details
        if compiled:
            mapped.setdefault(
                reaction_id,
                {
                    "genes": [],
                    "gpr_genes": [],
                    "ec": [],
                    "sources": [],
                    "score": 1.0,
                    "ambiguous": False,
                },
            )["gpr_rule"] = compiled
    return rule_details
