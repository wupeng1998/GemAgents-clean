"""Evidence tiers that retain ambiguity and avoid invented probabilities."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal

SourceClass = Literal[
    "imported_annotation",
    "curated_model_hit",
    "predicted_function",
    "reference_protein_match",
    "gapfill_hypothesis",
    "unresolved",
]


def evidence_source_class(route: str) -> SourceClass:
    normalized = route.casefold()
    if normalized == "genbank_annotation_import":
        return "imported_annotation"
    if normalized == "ncbi_hmm_equivalog":
        return "curated_model_hit"
    if normalized == "clean":
        return "predicted_function"
    if normalized == "reference_protein_match":
        return "reference_protein_match"
    if normalized == "gapfill_hypothesis":
        return "gapfill_hypothesis"
    return "unresolved"


@dataclass(frozen=True)
class EvidenceLink:
    protein_id: str
    sequence_sha256: str
    function_id: str
    reaction_id: str | None
    annotation_route: str
    source_class: SourceClass
    database_version: str
    model_accession: str | None
    thresholds: dict[str, float]
    identity: float | None
    coverage: float | None
    ambiguity: tuple[str, ...]
    complex_status: str
    compartment_support: str
    rejection_reason: str | None = None
    calibrated_probability: float | None = None
    calibration_label_set: str | None = None

    def __post_init__(self) -> None:
        if not self.protein_id or not self.function_id or not self.annotation_route:
            raise ValueError("Evidence links require protein, function and route")
        if len(self.sequence_sha256) != 64:
            raise ValueError("Evidence links require an input sequence SHA-256")
        if self.source_class != evidence_source_class(self.annotation_route):
            raise ValueError("annotation_route cannot silently upgrade source_class")
        if self.calibrated_probability is not None and not self.calibration_label_set:
            raise ValueError("Probability requires an independent calibration label set")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_annotation(
        cls,
        row: dict,
        sequence: str,
        function_id: str,
        *,
        reaction_id: str | None = None,
        ambiguity: tuple[str, ...] = (),
        rejection_reason: str | None = None,
    ) -> EvidenceLink:
        route = str(row.get("source", "unresolved"))
        thresholds = {
            key: float(row[key])
            for key in ("sequence_cutoff", "domain_cutoff")
            if row.get(key) is not None
        }
        return cls(
            protein_id=str(row.get("input_gene_id") or row.get("gene_id") or ""),
            sequence_sha256=hashlib.sha256(sequence.encode()).hexdigest(),
            function_id=function_id,
            reaction_id=reaction_id,
            annotation_route=route,
            source_class=evidence_source_class(route),
            database_version=str(row.get("database_version") or "unresolved"),
            model_accession=row.get("accession"),
            thresholds=thresholds,
            identity=row.get("identity"),
            coverage=row.get("coverage"),
            ambiguity=ambiguity,
            complex_status=str(row.get("complex_status", "unresolved")),
            compartment_support=str(row.get("compartment_support", "unresolved")),
            rejection_reason=rejection_reason,
        )
