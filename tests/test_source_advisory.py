"""来源为启发的新口径；来源提示不能绕过真实数学、答案或阶段合同。"""
import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_bottleneck import prepare
from test_library_questions import line, paper
from test_qc import setup_pipeline

from diamond_factory.config import Settings, load
from diamond_factory.consistency import consistency_input
from diamond_factory.difficulty import known_result_errors
from diamond_factory.gates import static_errors
from diamond_factory.library_questions import admit_lines
from diamond_factory.models import StepwiseBlueprintAudit
from diamond_factory.verifier import context_hash


def test_advisory_preset_retains_independent_scientific_guards():
    settings = load('configs/library-source-inspired.toml')
    assert settings.source_fidelity == 'advisory'
    assert settings.require_known_result and settings.require_stepwise_blueprint_review
    assert settings.require_verifier_adversary and settings.require_bottleneck_mask
    assert load('configs/library50-questions.toml').source_fidelity == 'strict'
    payload = settings.model_dump()
    payload['require_verifier_adversary'] = False
    with pytest.raises(ValidationError, match='Advisory provenance requires'):
        Settings.model_validate(payload)


def test_paraphrase_admitted_with_notes_and_without_scientific_pass():
    proposal = line().model_copy(update={'source_quote': 'A newly constructed synthetic model inspired by the source.'})
    allowed = {('Math', proposal.subtopic)}
    assert not admit_lines(paper(), [proposal], allowed, 10000)[0]
    selected, receipts = admit_lines(paper(), [proposal], allowed, 10000, source_fidelity='advisory')
    assert len(selected) == 1 and receipts[0]['source_notes'] and receipts[0]['anchor'] is None
    assert receipts[0]['scientific_pass'] is False
    proposal.pages[0].page = 99
    assert not admit_lines(paper(), [proposal], allowed, 10000, source_fidelity='advisory')[0]


@pytest.mark.parametrize('defect', ['answer', 'target', 'assumptions', 'source'])
def test_advisory_does_not_unlock_candidate_gold_or_model_premises(candidate, source, defect):
    prepare(candidate, source)
    candidate.known_result.evidence[0].quote = 'Inspiration can be paraphrased rather than copied.'
    assert not known_result_errors(candidate.known_result, {source.id: source}, candidate.solution,
                                   source_fidelity='advisory')
    if defect == 'answer':
        candidate.solution.answer = '37'
    elif defect == 'target':
        candidate.solution.design.target = 'A different constructed target.'
    elif defect == 'assumptions':
        candidate.solution.assumptions = []
    else:
        candidate.known_result.source_id = 'unregistered-source'
    assert known_result_errors(candidate.known_result, {source.id: source}, candidate.solution,
                               source_fidelity='advisory')


def test_blueprint_source_only_notes_are_soft_but_wrong_proof_is_hard():
    review = StepwiseBlueprintAudit(valid=True, answer_correct=True, derivation_complete=True,
        source_grounded=False, reason='The hypothetical model is independently valid, but not in the source.',
        reviewed_step_count=2, issues=[], source_notes=['A constructed result, not a source theorem.'])
    assert not review.passed() and review.passed(source_fidelity='advisory')
    review.answer_correct = False
    assert not review.passed(source_fidelity='advisory')


def test_static_citations_are_notes_but_unknown_source_and_wrong_gold_still_fail(candidate, source):
    quotas = json.loads(Path('configs/quotas.v3.json').read_text())
    candidate.source_fidelity = 'advisory'
    candidate.item.evidence[0].quote = 'A source-inspired construction with a nonliteral locator.'
    assert 'evidence_quote_not_in_source' not in static_errors(candidate, {source.id: source}, quotas)
    candidate.item.answer = 'a wrong answer different from the solution'
    assert 'solution_question_answer_mismatch' in static_errors(candidate, {source.id: source}, quotas)
    assert 'evidence_source_missing' in static_errors(candidate, {}, quotas)


def test_policy_change_invalidates_quality_context_and_cannot_relabel_old_question(
        tmp_path, settings, source, candidate):
    strict_consistency, strict_verifier = consistency_input(candidate, {source.id: source}), context_hash(candidate, {source.id: source})
    changed = candidate.model_copy(update={'source_fidelity': 'advisory'})
    assert consistency_input(changed, {source.id: source}) != strict_consistency
    assert context_hash(changed, {source.id: source}) != strict_verifier
    settings.source_fidelity = 'advisory'
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    assert not asyncio.run(pipe.preflight(candidate))
    assert 'source_policy_changed_requires_new_version' in candidate.reasons
    assert not gateway.calls


def test_answer_first_selection_allows_source_inspired_result_without_rebuying_quotes(
        tmp_path, settings, source, candidate):
    from diamond_factory.models import ResultSelection

    prepare(candidate, source)
    candidate.known_result.evidence[0].quote = 'A constructed variant inspired by this material rather than a literal quote.'
    settings.source_fidelity = 'advisory'
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def saved(role, prompt, salt, schema=None, **kwargs):
        gateway.calls.append(role)
        packet = json.loads(prompt.splitlines()[-1])
        assert role == 'generator' and packet['source_fidelity'] == 'advisory'
        return ResultSelection(outcome='found', result=candidate.known_result,
            reason='A candidate construction still requires independent full proof review.'), 'saved-response', 'fixture'

    gateway.call = saved
    selected = asyncio.run(pipe.select_known_result(source, source.model_dump(), {}, 'fixture', None))
    assert selected == candidate.known_result and gateway.calls == ['generator']
