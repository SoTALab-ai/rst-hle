"""可恢复的格式/对照损耗不改金标；真实科学错误仍阻断。"""
import asyncio
import json
from fractions import Fraction

import pytest
from pydantic import ValidationError
from test_bottleneck import enable, mask_review, prepare
from test_difficulty import step_review
from test_paper_ingest import pg as pg_fixture
from test_qc import setup_pipeline
from test_verifier_adversary import prepared

from diamond_factory.models import (
    BottleneckReview,
    LockedProofDraft,
    Question,
    ResultSelection,
    StepwiseBlueprintAudit,
    VerifierChallenges,
    VerifierDecision,
    VerifierLocatorPatches,
    VerifierOracle,
    digest,
)
from diamond_factory.pipeline import AuthoringRejected
from diamond_factory.substitution import Arithmetic, Exact
from diamond_factory.verifier import anchor_repairs_valid, round_errors


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def draft_for(solution):
    data = solution.model_dump(exclude={'answer', 'assumptions'})
    data['additional_assumptions'] = []
    data['design'].pop('target')
    return LockedProofDraft.model_validate(data)


@pytest.mark.parametrize('kind', ['inverse_identification', 'branch_discovery', 'parameter_region', 'theorem_applicability'])
def test_optional_initial_templates_preserve_locked_target_and_metadata(candidate, source, kind):
    prepare(candidate, source)
    draft = draft_for(candidate.solution)
    draft.design.construction_type = kind
    draft.design.bridge_type = 'representation'
    result = draft.bind(candidate.known_result)
    assert result.design.construction_type == kind and result.design.bridge_type == 'representation'
    assert result.design.target == candidate.known_result.target and result.answer == candidate.known_result.answer
    candidate.solution = result
    restored = type(candidate).model_validate_json(candidate.model_dump_json())
    assert restored.solution.design == result.design


def test_initial_template_references_reach_selection_before_target_lock(tmp_path, settings, source, candidate):
    enable(settings)
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    seen = []

    async def select(role, prompt, salt, schema=None, **kwargs):
        assert schema is ResultSelection
        packet = json.loads(prompt.splitlines()[-1])
        assert 'known_result' not in packet  # 目标尚未锁定，完整区域可以在这一阶段选择。
        references = packet['construction_reference']
        seen.extend(t['id'] for t in references['construction_templates'])
        assert {b['id'] for b in references['bridge_forms']} >= {'representation', 'auxiliary_object', 'invariant'}
        return ResultSelection(outcome='found', result=candidate.known_result,
            reason='A controlled initial target for the selection contract.'), 'r', 'gpt-6-luna'

    gateway.call = select
    selected = asyncio.run(pipe.select_known_result(source, source.model_dump(),
        {'category': 'Math', 'subtopic': '组合与图论'}, 'initial', None))
    assert selected == candidate.known_result
    assert set(seen) == {'inverse_identification', 'branch_discovery', 'parameter_region', 'theorem_applicability'}


def test_locked_schema_cannot_rewrite_answer_target_or_premises(candidate, source):
    prepare(candidate, source)
    candidate.known_result.answer += ' The private proof explains this result.'
    draft = draft_for(candidate.solution)
    result = draft.bind(candidate.known_result)
    assert result.answer == candidate.known_result.answer
    assert result.design.target == candidate.known_result.target
    assert result.assumptions == candidate.known_result.assumptions
    for key in ('answer', 'assumptions'):
        with pytest.raises(ValidationError):
            LockedProofDraft.model_validate({**draft.model_dump(), key: 'unrequested replacement'})
    with pytest.raises(ValidationError):
        LockedProofDraft.model_validate({**draft.model_dump(), 'design': {
            **draft.design.model_dump(), 'target': 'A replacement target'}})


@pytest.mark.parametrize('bad_proof', [False, True])
def test_locked_author_body_still_requires_independent_proof_before_alignment(
        tmp_path, settings, source, candidate, bad_proof):
    enable(settings)
    settings.use_locked_proof_draft = True
    settings.max_repairs = 0
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call
    seen = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        seen.append(schema)
        if schema is ResultSelection:
            return ResultSelection(outcome='found', result=candidate.known_result,
                reason='Synthetic immutable proposal used only for the contract.'), digest(salt), 'gpt-6-luna'
        if schema is LockedProofDraft:
            body = draft_for(candidate.solution)
            if bad_proof:
                body.derivation[-1] = 'The false identity 0=1 implies the proposed result.'
            return body, digest(salt), 'gpt-6-luna'
        if schema is StepwiseBlueprintAudit:
            data = json.loads(prompt.splitlines()[-1])
            assert data['solution']['answer'] == candidate.known_result.answer
            return step_review(passed=not bad_proof), digest(salt), 'gpt-6-luna'
        if schema is Question:
            return candidate.item, digest(salt), 'gpt-6-luna'
        if schema is BottleneckReview:
            return mask_review(), digest(salt), 'gpt-6-luna'
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    args = (source, None, {'category': 'Math', 'subtopic': '组合与图论'}, 0, 0)
    if bad_proof:
        with pytest.raises(AuthoringRejected):
            asyncio.run(pipe.generate(*args))
        assert Question not in seen
    else:
        c = asyncio.run(pipe.generate(*args))
        assert c.authoring_complete and c.item.answer == candidate.known_result.answer
    assert not any(role == 'solver' for role, _, _ in gateway.calls)


def test_large_exact_exponents_cover_paper_scales_but_remain_bounded():
    a = Arithmetic()
    assert a.expression('(1/16)**11') == Exact(Fraction(1, 16**11))
    assert a.expression('(1/16)**-10') == Exact(Fraction(16**10))
    assert a.expression('2**16') == Exact(Fraction(65536))
    for text in ('2**17', '2**-17', '2**1000000', '__import__("os")', '2**(16**16)'):
        with pytest.raises((ValueError, OverflowError)):
            a.expression(text)


@pytest.mark.parametrize('bad_golden', [False, True])
def test_verifier_failure_never_reenters_author_or_alignment(
        tmp_path, settings, source, candidate, bad_golden):
    from test_verifier_adversary import Adversary
    enable(settings)
    settings.use_locked_proof_draft = settings.require_verifier_adversary = True
    settings.max_repairs, settings.verifier_max_revisions = 1, 0
    prepare(candidate, source)
    pipe, _, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = Adversary(candidate)
    pipe.gateway = gateway
    original = gateway.call
    schemas = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        schemas.append(schema)
        if schema is ResultSelection:
            return ResultSelection(outcome='found', result=candidate.known_result,
                reason='Synthetic frozen result for stage-local repair.'), digest(salt), 'gpt-6-luna'
        if schema is LockedProofDraft:
            return draft_for(candidate.solution), digest(salt), 'gpt-6-luna'
        if schema is StepwiseBlueprintAudit:
            return step_review(), digest(salt), 'gpt-6-luna'
        if schema is BottleneckReview:
            return mask_review(), digest(salt), 'gpt-6-luna'
        if schema is Question:
            if ':repair:1:' in salt:
                data = json.loads(prompt.splitlines()[-1])
                assert data['repair_feedback']['errors'] and data['previous_question']
            return candidate.item.model_copy(deep=True), digest(salt), 'gpt-6-luna'
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is VerifierDecision and ':verifier:0:' in salt and salt.endswith(':apply:word'):
            value = value.model_copy(update={'correct': 'no'})
        if schema is VerifierOracle and bad_golden:
            value = value.model_copy(deep=True)
            value.golden_valid.verdict = 'fail'
        return value, rid, model

    gateway.call = call
    c = asyncio.run(pipe.generate(source, None, {'category': 'Math', 'subtopic': '组合与图论'}, 0, 0))
    assert schemas.count(Question) == 1
    assert schemas.count(LockedProofDraft) == schemas.count(StepwiseBlueprintAudit) == 1
    assert c.solution == candidate.solution
    assert c.status == 'rejected' and c.authoring_complete


@pytest.mark.parametrize('defect', ['shortfall', 'replace_case', 'change_oracle', 'bad_gold'])
def test_append_controls_without_discarding_hard_cases_or_fishing_for_gold(
        tmp_path, settings, source, candidate, defect):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is VerifierChallenges:
            data = json.loads(prompt.splitlines()[-1])
            if 'control_generation_feedback' not in data:
                value = value.model_copy(deep=True)
                value.cases = [value.cases[0], *value.cases[2:]]
                value.cases.append(value.cases[-1].model_copy(update={'id': 'other_wrong', 'response': '38'}))
            else:
                previous = VerifierChallenges.model_validate(data['control_generation_feedback']['previous_challenges'])
                old = previous.cases
                addition = value.cases[1]
                value.cases = [*old, addition]
                if defect == 'replace_case':
                    value.cases[0] = value.cases[0].model_copy(update={'response': 'changed old challenge'})
        if schema is VerifierOracle:
            value = value.model_copy(deep=True)
            for case in value.cases:
                if case.id == 'other_wrong':
                    case.verdict = 'no'
                if defect == 'change_oracle' and '-controls-repair:' in salt and case.id == 'wrong':
                    case.verdict = 'yes'
            if defect == 'bad_gold':
                value.golden_valid.verdict = 'fail'
        return value, rid, model

    gateway.call = call
    if defect in ('replace_case', 'change_oracle'):
        from diamond_factory.llm import ModelResponseError
        with pytest.raises(ModelResponseError):
            asyncio.run(pipe.preflight(candidate))
    else:
        assert asyncio.run(pipe.preflight(candidate)) == (defect == 'shortfall')
        result = candidate.verifier_assessment.versions[0].result
        if defect == 'shortfall':
            assert len(result.control_attempts) == 2 and not round_errors(result, candidate.item)
            assert result.challenges.cases[:4] == result.control_attempts[0].challenges.cases
            assert len(result.observations) == 5
            before = len(gateway.calls)
            assert asyncio.run(pipe.preflight(candidate)) and len(gateway.calls) == before
        else:
            assert len(result.control_attempts) == 1


def test_locator_patch_cannot_add_conditions_edit_rules_or_bypass_science(tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        if schema is VerifierLocatorPatches:
            return VerifierLocatorPatches.model_validate({'patches': [{'group': 'conditions', 'index': 0,
                'question_anchor': '37 vertices'}]}), digest(salt), 'gpt-6-luna'
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema.__name__ == 'VerifierSpec':
            value = value.model_copy(deep=True)
            value.conditions[0].question_anchor = 'Nonliteral grammatical paraphrase of the tree premise'
        return value, rid, model

    gateway.call = call
    assert asyncio.run(pipe.preflight(candidate))
    version = candidate.verifier_assessment.versions[0]
    assert anchor_repairs_valid(version)
    for field, replacement in [('request_id', ''), ('start', 0), ('literal', 'unrelated invented text')]:
        changed = version.model_copy(deep=True)
        setattr(changed.anchor_repairs[-1], field, replacement)
        assert not anchor_repairs_valid(changed)
    changed = version.model_copy(deep=True)
    changed.spec.conditions[0].condition = 'An additional scientifically unjustified condition'
    assert not anchor_repairs_valid(changed)


def test_pg_control_history_roundtrip_does_not_rebuy_completed_reviews(
        tmp_path, settings, source, candidate, pg):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    pg.add_source(source)
    pipe.store = pg
    pipe.start({'command': 'synthetic control persistence check'})
    assert asyncio.run(pipe.preflight(candidate))
    restored = pg.candidates()[0]
    history = restored.verifier_assessment.versions[0].result.control_attempts
    assert len(history) == 1 and history[0].challenge_request
    assert restored == candidate
    before = list(gateway.calls)
    assert asyncio.run(pipe.preflight(restored)) and gateway.calls == before


def test_frozen_verifier_cannot_modify_question_even_when_review_requests_it(
        tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate, 'question_repair')
    settings.freeze_question_at_verifier = True
    original = candidate.model_copy(deep=True)
    assert not asyncio.run(pipe.preflight(candidate))
    assert candidate.item == original.item and candidate.solution == original.solution
    assert 'upstream_defect_requires_new_version' in candidate.reasons[0]
    assert len(candidate.verifier_assessment.versions) == 1
    assert not any(':repair-question' in salt for _, _, salt in gateway.calls)
    calls = list(gateway.calls)
    assert not asyncio.run(pipe.preflight(candidate)) and gateway.calls == calls


def test_frozen_item_hash_invalidates_otherwise_passing_verifier(tmp_path, settings, source, candidate):
    from diamond_factory.verifier import has_valid_verifier
    pipe, _, _ = prepared(tmp_path, settings, source, candidate)
    assert asyncio.run(pipe.preflight(candidate))
    assert candidate.verifier_assessment.frozen_item_sha256 == digest(candidate.item.model_dump())
    altered = candidate.model_copy(deep=True)
    altered.verifier_assessment.versions[0].item.rationale += ' a hidden mutation'
    assert not has_valid_verifier(altered, {source.id: source})
