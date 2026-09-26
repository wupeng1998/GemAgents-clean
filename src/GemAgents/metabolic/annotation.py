"""Sequence input and deterministic annotation entry points."""

from GemAgents.metabolic.annotation_import import (
    metabolic_import_ncbi,
    metabolic_predict_genes,
)
from GemAgents.metabolic.hmm import metabolic_hmm_annotate, metabolic_prepare_hmms
from GemAgents.metabolic.predictions import metabolic_import_clean_predictions
from GemAgents.metabolic.sequence import (
    metabolic_detect_input,
    metabolic_fasta,
    metabolic_write_fasta,
)

__all__ = [
    "metabolic_detect_input",
    "metabolic_fasta",
    "metabolic_write_fasta",
    "metabolic_import_clean_predictions",
    "metabolic_hmm_annotate",
    "metabolic_import_ncbi",
    "metabolic_predict_genes",
    "metabolic_prepare_hmms",
]
