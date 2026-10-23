"""Mandatory pre-sampling correctness review, separate from HLE answer matching."""
from pathlib import Path

from .models import digest


class QualityRejected(RuntimeError):
    """A defective/uncertain item must not acquire difficulty measurements."""


def consistency_input(candidate, sources):
    return digest({"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(exclude_none=True),
                   "sources": {sid: sources[sid].sha256 for sid in candidate.source_ids}})


def verifier_fingerprint():
    return digest(Path(__file__).with_name("substitution.py").read_text())


def has_valid_consistency(candidate, sources):
    assessment = candidate.consistency
    evidence_ids = {e.source_id for e in candidate.item.evidence + candidate.solution.evidence}
    return bool(assessment and assessment.passed() and candidate.source_ids
                and assessment.verifier_sha256 == verifier_fingerprint()
                and evidence_ids <= set(candidate.source_ids)
                and set(candidate.source_ids) <= sources.keys()
                and assessment.input_sha256 == consistency_input(candidate, sources))
