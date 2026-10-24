"""Mandatory pre-sampling correctness review, separate from HLE answer matching."""
from pathlib import Path

from .difficulty import known_result_errors, valid_bottleneck
from .models import digest


class QualityRejected(RuntimeError):
    """A defective/uncertain item must not acquire difficulty measurements."""


def consistency_input(candidate, sources):
    data = {"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(exclude_none=True),
            "sources": {sid: sources[sid].sha256 for sid in candidate.source_ids}}
    if candidate.known_result:
        data["known_result"] = candidate.known_result.model_dump()
    return digest(data)


def verifier_fingerprint():
    return digest(Path(__file__).with_name("substitution.py").read_text())


def has_valid_consistency(candidate, sources):
    assessment = candidate.consistency
    if candidate.verifier_assessment:
        from .verifier import has_valid_verifier
        if not candidate.source_ids or not set(candidate.source_ids) <= sources.keys():
            return False
        if not has_valid_verifier(candidate, sources):
            return False
    evidence_ids = {e.source_id for e in candidate.item.evidence + candidate.solution.evidence}
    if candidate.known_result and known_result_errors(candidate.known_result, sources, candidate.solution):
        return False
    if (candidate.solution.design and candidate.solution.design.difficulty
            and candidate.solution.design.difficulty.mask and not valid_bottleneck(candidate, sources)):
        return False
    return bool(assessment and assessment.passed() and candidate.source_ids
                and assessment.verifier_sha256 == verifier_fingerprint()
                and evidence_ids <= set(candidate.source_ids)
                and set(candidate.source_ids) <= sources.keys()
                and assessment.input_sha256 == consistency_input(candidate, sources))
