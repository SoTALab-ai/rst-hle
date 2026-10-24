import asyncio
import json

import pytest
from pydantic import ValidationError
from test_difficulty import difficulty_payload, step_review, task_design
from test_qc import setup_pipeline

from diamond_factory.consistency import has_valid_consistency
from diamond_factory.difficulty import (
    bottleneck_mask_errors,
    construction_ready,
    known_result_errors,
)
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    BottleneckMask,
    BottleneckReview,
    DifficultyDesign,
    KnownResult,
    Question,
    ResultSelection,
    Solution,
    StepwiseBlueprintAudit,
)
from diamond_factory.pipeline import AuthoringRejected, QualityRejected


def enable(settings):
    settings.require_solution_design = True
    settings.require_difficulty_design = True
    settings.difficulty_profile_path = 'configs/difficulty-archetypes.json'
    settings.preverify_blueprint = True
    settings.require_stepwise_blueprint_review = True
    settings.require_known_result = True
    settings.require_bottleneck_mask = True


def prepare(candidate, source):
    candidate.known_result = KnownResult(source_id=source.id, locator='synthetic fixture',
        statement='A synthetic known result used only to verify the construction contract.',
        target='The number of edges in this synthetic tree.', answer=candidate.solution.answer,
        assumptions=list(candidate.solution.assumptions), evidence=candidate.solution.evidence)
    payload = difficulty_payload()
    payload['mask'] = {'step_id': 'singularities', 'proof_step_indices': [1],
        'hardest_reason': 'A synthetic comparison of the selected inference against routine arithmetic.',
        'alternative_steps': ['The arithmetic target is simpler than the synthetic structural inference.'],
        'hidden_information': ['The synthetic bridge that determines the target.'],
        'retained_information': ['The original defining observations in the fixture.'],
        'recoverability': 'The retained synthetic premises entail the fixed target by the complete reference proof.'}
    candidate.solution.design = task_design(DifficultyDesign.model_validate(payload))
    candidate.solution.design.target = candidate.known_result.target
    candidate.blueprint_audit = step_review()
    return candidate


def stamp(pipe, candidate, source):
    candidate.blueprint_fingerprint = pipe.blueprint_stamp(candidate.solution,
        {**source.model_dump(), 'text': source.text[:pipe.settings.max_source_chars]},
        {'category': candidate.item.category, 'subtopic': candidate.item.subtopic}, candidate.known_result)


def mask_review(verdict='pass', step_id='singularities'):
    check = {'verdict': 'pass', 'reasoning': 'A controlled fixture check, not a measured hardness claim.',
                 'witness': 'The synthetic check isolates the masking contract only.'}
    payload = {name: dict(check) for name in BottleneckReview.check_names()}
    payload['hidden']['verdict'] = verdict
    return BottleneckReview(step_id=step_id, **payload)


def test_source_selection_must_explicitly_have_or_decline_a_result():
    with pytest.raises(ValidationError):
        ResultSelection(outcome='found', result=None, reason='A result was not actually available.')
    assert ResultSelection(outcome='unavailable', result=None, reason='The excerpt lacks a usable conclusion.')


@pytest.mark.parametrize('defect', ['answer', 'target', 'assumptions', 'quote'])
def test_locked_source_result_cannot_silently_change(candidate, source, defect):
    prepare(candidate, source)
    if defect == 'answer':
        candidate.solution.answer = '37'
    elif defect == 'target':
        candidate.solution.design.target = 'A different target chosen after locking the answer.'
    elif defect == 'assumptions':
        candidate.solution.assumptions = []
    else:
        candidate.known_result.evidence[0].quote = 'This evidence does not occur anywhere in the source.'
    assert known_result_errors(candidate.known_result, {source.id: source}, candidate.solution)


@pytest.mark.parametrize('indices', [[0], [1, 1]])
def test_mask_proof_indices_are_strict_and_unique(candidate, source, indices):
    prepare(candidate, source)
    mask = candidate.solution.design.difficulty.mask.model_dump()
    mask['proof_step_indices'] = indices
    with pytest.raises(ValidationError):
        BottleneckMask.model_validate(mask)


def test_mask_must_target_the_selected_bottleneck_in_the_complete_proof(candidate, source):
    prepare(candidate, source)
    mask = candidate.solution.design.difficulty.mask
    mask.step_id = 'target'
    mask.proof_step_indices = [3]
    assert set(bottleneck_mask_errors(candidate.solution)) == {
        'mask_does_not_target_selected_bottleneck', 'masked_step_outside_full_proof'}


def test_known_result_is_selected_once_and_frozen_during_proof_repairs(tmp_path, settings, source, candidate):
    enable(settings)
    settings.max_repairs = 2
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    sequence, plans = [], []

    async def call(role, prompt, salt, schema=None, **kwargs):
        sequence.append(schema)
        if schema is ResultSelection:
            return ResultSelection(outcome='found', result=candidate.known_result,
                                   reason='Controlled existing-result extraction.'), 'result', 'gpt-6-luna'
        if schema is Solution:
            data = json.loads(prompt.splitlines()[-1])
            assert data['known_result'] == candidate.known_result.model_dump()
            plans.append(data)
            return candidate.solution, 'author', 'gpt-6-luna'
        if schema is StepwiseBlueprintAudit:
            data = json.loads(prompt.splitlines()[-1])
            assert data['known_result']['answer'] == '36'
            return step_review(passed=len(plans) == 2), 'judge', 'gpt-6-luna'
        assert schema is Question
        raise OSError('Stop at approved-proof question generation')

    gateway.call = call
    with pytest.raises(OSError, match='approved-proof'):
        asyncio.run(pipe.generate(source, None, {'category': 'Math', 'subtopic': '组合与图论'}, 0, 0))
    assert sequence == [ResultSelection, Solution, StepwiseBlueprintAudit, Solution, StepwiseBlueprintAudit, Question]
    assert plans[1]['feedback']['blueprint_review']['issues'][0]['step_index'] == 2


def test_changed_gold_is_rejected_before_question_even_if_judge_approves(tmp_path, settings, source, candidate):
    enable(settings)
    settings.max_repairs = 0
    prepare(candidate, source)
    wrong = candidate.solution.model_copy(update={'answer': '37'})
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def call(role, prompt, salt, schema=None, **kwargs):
        assert schema is not Question
        if schema is ResultSelection:
            return ResultSelection(outcome='found', result=candidate.known_result,
                                   reason='Controlled existing-result extraction.'), 'result', 'gpt-6-luna'
        if schema is Solution:
            return wrong, 'author', 'gpt-6-luna'
        return step_review(), 'judge', 'gpt-6-luna'

    gateway.call = call
    with pytest.raises(AuthoringRejected, match='locked_result_answer_changed'):
        asyncio.run(pipe.generate(source, None, {'category': 'Math', 'subtopic': '组合与图论'}, 0, 0))


def test_unavailable_source_is_not_replaced_with_an_invented_gold(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def call(*args, **kwargs):
        return ResultSelection(outcome='unavailable', result=None, reason='The source excerpt lacks a proven result.'), 'r', 'gpt-6-luna'

    gateway.call = call
    with pytest.raises(AuthoringRejected, match='known_result_unavailable'):
        asyncio.run(pipe.select_known_result(source, source.model_dump(), {}, 'fixture', None))


@pytest.mark.parametrize('missing', ['result', 'mask', 'binding'])
def test_new_required_construction_cannot_bypass_paid_sampling(tmp_path, settings, source, candidate, missing):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    stamp(pipe, candidate, source)
    if missing == 'result':
        candidate.known_result = None
    elif missing == 'mask':
        candidate.solution.design.difficulty.mask = None
    else:
        candidate.blueprint_fingerprint = ''
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(candidate, 4))
    assert not gateway.calls and not candidate.trials


@pytest.mark.parametrize('verdict', ['fail', 'uncertain'])
def test_bottleneck_leak_or_unresolved_recoverability_blocks_glm(tmp_path, settings, source, candidate, verdict):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    stamp(pipe, candidate, source)

    async def call(role, prompt, salt, schema=None, **kwargs):
        gateway.calls.append(schema)
        assert schema is BottleneckReview
        return mask_review(verdict), 'mask', 'gpt-6-luna'

    gateway.call = call
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(candidate, 4))
    assert gateway.calls == [BottleneckReview] and not candidate.trials
    assert candidate.status == 'rejected' and candidate.bottleneck_assessment


@pytest.mark.parametrize('change', ['question', 'proof', 'prompt', 'model'])
def test_mask_receipt_reused_only_for_identical_inputs(tmp_path, settings, source, candidate, change):
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    calls = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        calls.append(schema)
        return mask_review(), 'mask', 'gpt-6-luna'

    gateway.call = call
    sources = {source.id: source}
    assert asyncio.run(pipe.review_bottleneck(candidate, sources))
    assert asyncio.run(pipe.review_bottleneck(candidate, sources))
    assert len(calls) == 1
    if change == 'question':
        candidate.item.question += ' Here is the previously hidden synthetic lemma.'
    elif change == 'proof':
        candidate.solution.derivation[0] += ' Modified proof input.'
    elif change == 'prompt':
        pipe.prompts['bottleneck_audit'] += '\nModified review contract.'
    else:
        settings.consistency.reasoning_effort = 'medium'
    assert asyncio.run(pipe.review_bottleneck(candidate, sources))
    assert len(calls) == 2


def test_review_mapping_failure_is_not_a_scientific_rejection(tmp_path, settings, source, candidate):
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def call(*args, **kwargs):
        return mask_review(step_id='wrong_step'), 'bad-map', 'gpt-6-luna'

    gateway.call = call
    with pytest.raises(ModelResponseError, match='invalid_bottleneck_step_mapping'):
        asyncio.run(pipe.review_bottleneck(candidate, {source.id: source}))
    assert candidate.status == 'quality_seed' and not candidate.trials


def test_export_requires_current_locked_proof_and_mask_receipt(tmp_path, settings, source, candidate):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    stamp(pipe, candidate, source)

    async def call(*args, **kwargs):
        return mask_review(), 'mask', 'gpt-6-luna'

    gateway.call = call
    sources = {source.id: source}
    assert not construction_ready(candidate, sources, settings, pipe.prompts)
    assert asyncio.run(pipe.review_bottleneck(candidate, sources))
    assert construction_ready(candidate, sources, settings, pipe.prompts)
    candidate.solution.derivation[0] += ' Modified after approval.'
    assert not construction_ready(candidate, sources, settings, pipe.prompts)
    assert not has_valid_consistency(candidate, sources)


def test_false_locked_result_stops_instead_of_silently_replacing_it(tmp_path, settings, source, candidate):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    seen = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        seen.append(schema)
        if schema is ResultSelection:
            return ResultSelection(outcome='found', result=candidate.known_result,
                                   reason='Controlled source extraction.'), 'r', 'gpt-6-luna'
        if schema is Solution:
            return candidate.solution, 'a', 'gpt-6-luna'
        audit = step_review(passed=False)
        audit.issues[0].part = 'known_result'
        return audit, 'j', 'gpt-6-luna'

    gateway.call = call
    with pytest.raises(AuthoringRejected, match='locked_known_result_rejected'):
        asyncio.run(pipe.generate(source, None, {'category': 'Math', 'subtopic': '组合与图论'}, 0, 0))
    assert seen == [ResultSelection, Solution, StepwiseBlueprintAudit]


def test_model_failure_during_mask_review_is_not_wrong_answer_or_quality_verdict(tmp_path, settings, source, candidate):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    stamp(pipe, candidate, source)

    async def call(*args, **kwargs):
        raise ModelResponseError('consistency', 'timeout', 'deadline_exceeded')

    gateway.call = call
    with pytest.raises(ModelResponseError, match='deadline_exceeded'):
        asyncio.run(pipe.sample(candidate, 4))
    assert candidate.status == 'quality_seed' and not candidate.trials and not candidate.bottleneck_assessment


def test_resume_reuses_locked_selection_and_returns_mask_failure_feedback(tmp_path, settings, source, candidate):
    enable(settings)
    prepare(candidate, source)
    candidate.operator = 'bootstrap'
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    target = {'category': 'Math', 'subtopic': '组合与图论'}
    candidate.id = pipe.generation_identity(source, None, target, 0, 0)
    candidate.root_id = candidate.id
    candidate.status = 'rejected'
    candidate.authoring_complete = False
    candidate.authoring_repairs_completed = 1
    store.save(candidate)

    async def call(role, prompt, salt, schema=None, **kwargs):
        assert schema is Solution
        data = json.loads(prompt.splitlines()[-1])
        assert data['known_result'] == candidate.known_result.model_dump()
        assert data['feedback']['previous_solution'] == candidate.solution.model_dump()
        assert 'bottleneck_assessment' in data['feedback']
        raise OSError('Captured resumed author feedback without reselecting the result')

    gateway.call = call
    with pytest.raises(OSError, match='without reselecting'):
        asyncio.run(pipe.generate(source, None, target, 0, 0))
