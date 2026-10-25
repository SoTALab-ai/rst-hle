import json

import pytest
from conftest import passing_consistency, trials
from pydantic import ValidationError
from test_bottleneck import mask_review, prepare

from diamond_factory.easy_audit import ManualFindings, summarize_easy_cohort
from diamond_factory.llm import CacheIntegrityError
from diamond_factory.models import (
    BottleneckAssessment,
    ConsistencyAssessment,
    IndependentSolution,
    SubstitutionReport,
    digest,
)


def evidence(candidate, source):
    prepare(candidate, source)
    candidate.trials = trials(16)[:4]
    candidate.status = 'rejected'
    candidate.reasons = ['too_easy:first_four_all_correct']
    candidate.consistency = ConsistencyAssessment(input_sha256='fixture', prompt_hashes={},
        model_config_sha256='fixture', independent_solution=IndependentSolution(outcome='solved',
            derivation=list(candidate.solution.derivation), answer=candidate.item.answer, issues=[]),
        audit=passing_consistency(), independent_request='independent', review_request='review',
        independent_actual_model='fixture', review_actual_model='fixture',
        substitution=SubstitutionReport(verdict='pass', results=[], uncovered_outputs=[], reason='fixture'))
    candidate.bottleneck_assessment = BottleneckAssessment(input_sha256='fixture', prompt_sha256='fixture',
        model_config_sha256='fixture', review=mask_review(), request_id='mask', actual_model='fixture')
    traces = {}

    def seal(request_id, role, content, model):
        trace = {'request_id': request_id, 'role': role, 'content': content, 'actual_model': model}
        trace['integrity_sha256'] = digest(trace)
        traces[request_id] = trace

    for trial in candidate.trials:
        seal(trial.solver_request, 'solver', trial.response, trial.solver_actual_model)
        seal(trial.judge_request, 'judge', json.dumps(trial.judgment.model_dump()), trial.judge_actual_model)
    cs = candidate.consistency
    seal(cs.independent_request, 'consistency', json.dumps(cs.independent_solution.model_dump()),
         cs.independent_actual_model)
    findings = ManualFindings(method='Synthetic audit protocol fixture, not a difficulty claim.',
        labels={'shallow_target': 'synthetic'}, cases=[{
            'index': 1, 'root': 'root1', 'cause': 'shallow_target', 'mechanism': 'synthetic mechanism',
            'observation': 'Synthetic manually entered finding used to verify evidence binding only.'}])
    return traces, findings


def test_read_only_audit_binds_every_saved_answer_and_keeps_text_private(candidate, source):
    traces, findings = evidence(candidate, source)
    before = candidate.model_dump()
    result = summarize_easy_cohort([candidate], traces, {candidate.id: 'root1'}, findings)
    assert result['case_count'] == 1 and result['existing_glm_answers_read'] == 4
    assert result['primary_causes'] == {'shallow_target': 1}
    assert result['new_http_requests'] == 0
    assert candidate.model_dump() == before
    assert candidate.item.question not in json.dumps(result)
    assert candidate.trials[0].response not in json.dumps(result)


@pytest.mark.parametrize('defect', ['changed_seal', 'different_answer', 'judge', 'blind_solution', 'missing'])
def test_changed_or_missing_paid_evidence_is_rejected(candidate, source, defect):
    traces, findings = evidence(candidate, source)
    trial = candidate.trials[0]
    if defect == 'missing':
        del traces[trial.solver_request]
    elif defect == 'different_answer':
        candidate.trials[0].response += ' Changed stored candidate only.'
    elif defect == 'blind_solution':
        candidate.consistency.independent_solution.answer += ' Changed stored independent solution.'
    else:
        trace = traces[trial.judge_request if defect == 'judge' else trial.solver_request]
        trace['content'] += ' changed'
    with pytest.raises(CacheIntegrityError):
        summarize_easy_cohort([candidate], traces, {candidate.id: 'root1'}, findings)


@pytest.mark.parametrize('defect', ['partial', 'incorrect', 'sample_id', 'duplicate_request', 'coverage'])
def test_incomplete_cohort_and_reused_answers_cannot_inflate_audit(candidate, source, defect):
    traces, findings = evidence(candidate, source)
    root_names = {candidate.id: 'root1'}
    if defect == 'partial':
        candidate.trials.pop()
    elif defect == 'incorrect':
        candidate.trials[0].judgment.correct = 'no'
    elif defect == 'sample_id':
        candidate.trials[1].sample_id = 0
    elif defect == 'duplicate_request':
        candidate.trials[1].solver_request = candidate.trials[0].solver_request
    else:
        root_names = {}
    with pytest.raises((ValueError, CacheIntegrityError)):
        summarize_easy_cohort([candidate], traces, root_names, findings)


def test_manual_semantic_labels_cannot_be_replaced_by_unknown_tags(candidate, source):
    _, findings = evidence(candidate, source)
    payload = findings.model_dump()
    payload['cases'][0]['cause'] = 'keyword_based_guess'
    with pytest.raises(ValidationError):
        ManualFindings.model_validate(payload)
