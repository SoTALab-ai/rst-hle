"""RST父子题净负担检查：结构增量不能抵销其他维度的简化。"""
import json

from .models import RstNonRegressionAudit, digest


def proposal_contract(candidate):
    return digest({'known_result': candidate.known_result.model_dump(),
        'solution': candidate.solution.model_dump(), 'required_outputs': candidate.item.required_outputs,
        'category': candidate.item.category, 'subtopic': candidate.item.subtopic})


def question_contract(candidate):
    return digest({'proposal': proposal_contract(candidate), 'item': candidate.item.model_dump()})


def regression_errors(review, phase):
    if review.phase != phase:
        return ['non_regression_unverified:phase_mismatch']
    errors = [f'non_regression_{x.verdict}:{phase}:{x.axis}' for x in review.comparisons
              if x.verdict in ('reduced', 'uncertain')]
    if review.net_verdict != 'no_offset':
        errors.append(f'non_regression_{review.net_verdict}:{phase}:overall')
    return errors


def valid_non_regression(candidate):
    origin = candidate.rst_origin
    evidence = origin.non_regression if origin else None
    return bool(evidence and origin.parent_snapshot
        and evidence.parent_sha256 == digest(origin.parent_snapshot.model_dump())
        and evidence.proposal_sha256 == proposal_contract(candidate)
        and evidence.question_sha256 == question_contract(candidate)
        and evidence.proposal.phase == 'proposal' and evidence.question.phase == 'question'
        and evidence.proposal.passed() and evidence.question.passed()
        and evidence.proposal_request and evidence.proposal_actual_model
        and evidence.question_request and evidence.question_actual_model)


def comparison_packet(parent, *, phase, solution, known_result, required_outputs, category, subtopic, item=None):
    return {'phase': phase, 'parent': parent.model_dump(), 'child': {
        'solution': solution.model_dump(), 'known_result': known_result.model_dump(),
        'required_outputs': required_outputs, 'category': category, 'subtopic': subtopic,
        'item': item.model_dump() if item else None}}


async def review_non_regression(call, prompt, parent, *, salt, **child):
    """正式管线与固定样本诊断共用同一提示、严格schema和失败处理。"""
    packet = comparison_packet(parent, **child)
    return await call(prompt + '\n' + json.dumps(packet, ensure_ascii=False), salt, RstNonRegressionAudit)
