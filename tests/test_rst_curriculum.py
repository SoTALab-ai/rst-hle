"""受控协议测试，不将fixture的假增长/假审查当真实难度证据。"""
import asyncio
import json

import pytest
from pydantic import ValidationError
from test_bottleneck import prepare
from test_difficulty import step_review
from test_difficulty_adversary import DepthGateway

from diamond_factory.config import load
from diamond_factory.difficulty import construction_ready
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    ConsistencyAudit,
    LockedProofDraft,
    LockedTaskDesign,
    RstGrowthAudit,
    StepwiseBlueprintAudit,
    digest,
)
from diamond_factory.pipeline import Pipeline
from diamond_factory.rst_curriculum import (
    RstCurriculum,
    RstPlan,
    RstProposal,
    RstQuestionDraft,
    RstQuestionRepair,
    RstSeed,
    rst_protocol,
    valid_rst_origin,
)
from diamond_factory.store import Store


class RstGateway(DepthGateway):
    def __init__(self, candidate, counts=None, growth=True, science=True, bad_source=False, bad_topic=False):
        super().__init__(candidate)
        self.counts, self.growth, self.science = counts or {}, growth, science
        self.bad_source, self.bad_topic = bad_source, bad_topic
        self.generations = {}

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        if role == 'solver':
            self.calls.append((role, prompt, salt))
            cid = salt.split(':')[0]
            sample = int(salt.rsplit(':', 1)[1])
            n = self.counts.get(self.generations.get(cid, 0), 16)
            ok = n == 16 or 1 <= sample <= n
            return 'Final Answer: ' + ('36' if ok else '37'), digest(salt), 'glm-5.2'
        if schema not in (RstProposal, RstGrowthAudit, RstQuestionDraft, StepwiseBlueprintAudit):
            return await super().call(role, prompt, salt, schema, **kwargs)
        if salt in self.cache:
            return self.cache[salt]
        data = json.loads(prompt.splitlines()[-1])
        self.calls.append((role, prompt, salt))
        if schema is RstProposal:
            self.generations[salt.split(':')[0]] = data['generation']
            result = self.candidate.known_result.model_copy(deep=True)
            if self.bad_source:
                result.source_id = 'unregistered-source'
            proof = LockedProofDraft(topic=self.candidate.solution.topic, additional_assumptions=[],
                derivation=self.candidate.solution.derivation + ['Controlled new dependency fixture.'],
                evidence=self.candidate.solution.evidence, reasoning_delta='A controlled extra dependency for transport tests.',
                design=LockedTaskDesign.model_validate(self.candidate.solution.design.model_dump(exclude={'target'})))
            value = RstProposal(reason='Controlled growth test, not scientific evidence.',
                subtopic='not-a-v3-label' if self.bad_topic else self.candidate.item.subtopic,
                extension='Controlled necessary dependency; the test does not assert real mathematical depth.',
                result=result, proof=proof, required_outputs=['edge count'])
        elif schema is RstGrowthAudit:
            value = RstGrowthAudit(parent_core_preserved=True, necessary_new_dependency=self.growth,
                substantive_change=self.growth, compact_target=True, parent_proof_entries=[1],
                child_proof_entries=[2], shortest_correct_route=['Controlled equivalent fixture route.'],
                reason='Controlled growth decision used to test lineage and bounds only.')
        elif schema is RstQuestionDraft:
            value = RstQuestionDraft(question=self.candidate.item.question + ' Controlled version ' + salt[:24] + '.',
                rationale=self.candidate.item.rationale, material_key='controlled-rst-version')
        else:
            if self.science is None:
                raise ModelResponseError(role, digest(salt), 'invalid_schema')
            value = step_review(count=len(data['solution']['derivation']))
            if not self.science:
                from diamond_factory.models import BlueprintIssue
                value.valid = False
                value.issues = [BlueprintIssue(part='derivation', step_index=1,
                    claim='The controlled fixture has an unproved claim.', evidence='Controlled false proof fixture.',
                    repair_requirement='Repair the scientific claim, not its appearance.')]
        answer = value, digest(salt), 'gpt-6-luna'
        self.cache[salt] = answer
        return answer


def setup_rst(tmp_path, source, candidate, *, rounds=3, store=None, **kwargs):
    prepare(candidate, source)
    settings = load('configs/rst-curriculum.toml')
    store = store or Store(tmp_path / 'rst.sqlite')
    store.add_source(source)
    spec = RstSeed(name='fixture', candidate=candidate.model_copy(deep=True), sha256=digest(candidate.model_dump()))
    plan = RstPlan(seeds=[spec], growth_rounds=rounds)
    gateway = RstGateway(candidate.model_copy(deep=True), **kwargs)
    pricing = {'observed_at': 'controlled-test', 'rmb_reference_exchange_rate': 6.7, 'rates_per_million': {}}
    pipe = RstCurriculum(settings, store, 'rst-test', 'pilot', tmp_path / 'run', gateway, plan=plan, pricing=pricing)
    return pipe, store, gateway


def test_correct_easy_seed_grows_three_rounds_without_mutating_parent_and_resumes(tmp_path, source, candidate):
    pipe, store, gateway = setup_rst(tmp_path, source, candidate)
    before = pipe.plan.seeds[0].model_dump()
    report = asyncio.run(pipe.run_curriculum())
    assert report['formed_versions'] == 4 and report['retained_lineages'] == 0
    rows = report['lines'][0]['rounds']
    assert [r['generation'] for r in rows] == [0, 1, 2, 3]
    assert all(r['disposition'] == 'easy_research_seed' and r['samples'] == 4 for r in rows)
    own = store.candidates()
    assert len({c.root_id for c in own}) == 1
    assert all(own[i].parent_id == own[i-1].id for i in range(1, 4))
    assert all(valid_rst_origin(c, {source.id: source}, pipe.protocol) for c in own)
    assert own[0].item == candidate.item and own[0].solution == candidate.solution
    assert before == pipe.plan.seeds[0].model_dump()
    count = len(gateway.calls)
    resumed = RstCurriculum(pipe.settings, store, pipe.run_id, 'pilot', pipe.run_dir, gateway,
        plan=pipe.plan, pricing=pipe.pricing)
    assert asyncio.run(resumed.run_curriculum())['formed_versions'] == 4
    assert len(gateway.calls) == count


@pytest.mark.parametrize('count,outcome', [(8, 'in_band'), (0, 'too_hard_stop'), (12, 'easy_research_seed')])
def test_four_to_sixteen_and_stopping(tmp_path, source, candidate, count, outcome):
    pipe, store, gateway = setup_rst(tmp_path, source, candidate, rounds=1, counts={1: count})
    report = asyncio.run(pipe.run_curriculum())
    last = report['lines'][0]['rounds'][-1]
    assert last['samples'] == 16 and last['correct'] == count and last['disposition'] == outcome
    assert sum(role == 'solver' for role, _, _ in gateway.calls) == 20
    assert len(store.candidates()) == 2


@pytest.mark.parametrize('fault', ['growth', 'science', 'bad_source', 'bad_topic'])
def test_failed_growth_or_contract_never_buys_child_samples(tmp_path, source, candidate, fault):
    kwargs = {fault: not fault in ('growth', 'science')}
    pipe, _, gateway = setup_rst(tmp_path, source, candidate, **kwargs)
    report = asyncio.run(pipe.run_curriculum())
    if fault == 'science':
        assert report['formed_versions'] == 0 and not any(r == 'solver' for r, _, _ in gateway.calls)
    else:
        assert report['formed_versions'] == 1
        assert sum(r == 'solver' for r, _, _ in gateway.calls) == 4
        assert report['lines'][0]['rounds'][1]['proposal_attempts'] == 3


def test_model_uncertainty_is_unverified_not_wrong_answer(tmp_path, source, candidate):
    pipe, store, gateway = setup_rst(tmp_path, source, candidate, science=None)
    result = asyncio.run(pipe.run_curriculum())
    assert result['formed_versions'] == 0 and store.runs()[pipe.run_id]['status'] == 'completed_with_errors'
    assert not any(role == 'solver' for role, _, _ in gateway.calls)


def test_origin_is_required_at_generic_sampling_and_export_entry(tmp_path, source, candidate):
    pipe, store, gateway = setup_rst(tmp_path, source, candidate, rounds=1)
    asyncio.run(pipe.run_curriculum())
    c = store.candidates()[-1]
    # 此处去掉终态拒绝，以验证真正的合同防线，不靠status提前返回。
    c.status = 'quality_seed'
    c.item.question += ' Unrecorded change.'
    count = len(gateway.calls)
    generic = Pipeline(pipe.settings, store, 'bypass', 'pilot', tmp_path / 'bypass', gateway)
    assert not asyncio.run(generic.preflight(c))
    assert 'missing_or_stale_rst_growth_contract' in c.reasons
    assert not construction_ready(c, {source.id: source}, pipe.settings, pipe.prompts)
    assert len(gateway.calls) == count


def test_progress_corruption_and_changed_plan_refuse_resume(tmp_path, source, candidate):
    pipe, store, gateway = setup_rst(tmp_path, source, candidate, rounds=1)
    asyncio.run(pipe.run_curriculum())
    changed = pipe.plan.model_copy(update={'growth_rounds': 2})
    with pytest.raises(ValueError, match='plan changed'):
        RstCurriculum(pipe.settings, store, pipe.run_id, 'pilot', pipe.run_dir, gateway, plan=changed, pricing=pipe.pricing)
    (pipe.objects.root / pipe.rst_pointer[0]).write_text('corrupt')
    with pytest.raises(ValueError, match='corrupt'):
        RstCurriculum(pipe.settings, store, pipe.run_id, 'pilot', pipe.run_dir, gateway, plan=pipe.plan, pricing=pipe.pricing)


def test_endpoint_change_invalidates_origin_and_standard_policy_is_unchanged(tmp_path, source, candidate, monkeypatch):
    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    asyncio.run(pipe.run_curriculum())
    c = store.candidates()[-1]
    monkeypatch.setenv('NEW_API_BASE_URL', 'https://different.invalid')
    assert not valid_rst_origin(c, {source.id: source}, rst_protocol(pipe.settings, pipe.prompts))
    assert load('configs/library-difficulty-adversary.toml').synthesis_protocol == 'standard'
    broken = pipe.settings.model_dump()
    broken['require_verifier_adversary'] = False
    with pytest.raises(ValidationError):
        type(pipe.settings).model_validate(broken)


class ContractRepairGateway(RstGateway):
    def __init__(self, candidate, outcome='question_contract_only'):
        super().__init__(candidate)
        self.outcome = outcome
        self.bad_contract_seen = False

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        if schema is RstQuestionRepair:
            self.calls.append((role, prompt, salt))
            value = RstQuestionRepair(outcome=self.outcome, reason='The controlled question adds an unrequested proof output.',
                revised=RstQuestionDraft(question=self.candidate.item.question + ' Return only the edge count.',
                    rationale=self.candidate.item.rationale, material_key='controlled-contract-repair')
                    if self.outcome == 'question_contract_only' else None)
            return value, digest(salt), 'gpt-6-luna'
        value = await super().call(role, prompt, salt, schema, **kwargs)
        if (schema is ConsistencyAudit and self.generations.get(salt.split(':')[0], 0) > 0
                and not self.bad_contract_seen):
            self.bad_contract_seen = True
            review = value[0].model_copy(deep=True)
            review.definitions.verdict = 'fail'
            review.definitions.reasoning = 'The public request adds proof output, but the fixed gold is only 36.'
            return review, value[1], value[2]
        return value


def test_question_contract_repair_preserves_locked_outputs_gold_and_full_proof(tmp_path, source, candidate):
    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    pipe.gateway = gateway = ContractRepairGateway(candidate.model_copy(deep=True))
    result = asyncio.run(pipe.run_curriculum())
    child = store.candidates()[-1]
    attempt = pipe.progress.lines['fixture'][1].attempts[-1]
    old = attempt.question_versions[0]
    assert child.item.answer == old.item.answer and child.solution == old.solution
    assert child.item.required_outputs == old.item.required_outputs == ['edge count']
    assert child.item != old.item and valid_rst_origin(child, {source.id: source}, pipe.protocol)
    assert result['lines'][0]['rounds'][1]['question_repairs'] == 1
    assert sum(':rst-question-repair:' in s for _, _, s in gateway.calls) == 1
    assert sum(role == 'solver' for role, _, _ in gateway.calls) == 8


def test_question_repair_crash_recovers_saved_decision_without_rebuy(tmp_path, source, candidate):
    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    gateway = ContractRepairGateway(candidate.model_copy(deep=True))
    pipe.gateway = gateway
    audit = pipe.audit_item
    crashed = False

    async def crash_after_saved_decision(c):
        nonlocal crashed
        if c.authoring_repairs_completed and not crashed:
            crashed = True
            raise RuntimeError('simulated crash after durable repair decision')
        return await audit(c)

    pipe.audit_item = crash_after_saved_decision
    with pytest.raises(RuntimeError, match='simulated crash'):
        asyncio.run(pipe.run_curriculum())
    assert sum(':rst-question-repair:' in s for _, _, s in gateway.calls) == 1
    resumed = RstCurriculum(pipe.settings, store, pipe.run_id, 'pilot', pipe.run_dir, gateway,
        plan=pipe.plan, pricing=pipe.pricing)
    result = asyncio.run(resumed.run_curriculum())
    assert result['lines'][0]['rounds'][1]['samples'] == 4
    assert sum(':rst-question-repair:' in s for _, _, s in gateway.calls) == 1


@pytest.mark.parametrize('outcome', ['proof_or_gold_defect', 'uncertain'])
def test_gold_defects_or_uncertainty_cannot_be_repaired_by_changing_question(tmp_path, source, candidate, outcome):
    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    gateway = ContractRepairGateway(candidate.model_copy(deep=True), outcome)
    pipe.gateway = gateway
    result = asyncio.run(pipe.run_curriculum())
    child = store.candidates()[-1]
    assert not child.trials and child.item.answer == '36'
    assert result['lines'][0]['rounds'][1]['disposition'] == 'quality_rejected_or_unverified'
    assert sum(':rst-question-repair:' in s for _, _, s in gateway.calls) == 1


def test_persistent_question_failure_has_two_repair_cap(tmp_path, source, candidate):
    class Persistent(ContractRepairGateway):
        async def call(self, role, prompt, salt, schema=None, **kwargs):
            if schema is ConsistencyAudit:
                self.bad_contract_seen = False
            return await super().call(role, prompt, salt, schema, **kwargs)

    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    pipe.gateway = gateway = Persistent(candidate.model_copy(deep=True))
    result = asyncio.run(pipe.run_curriculum())
    assert result['lines'][0]['rounds'][1]['question_repairs'] == 2
    assert not store.candidates()[-1].trials
    assert sum(':rst-question-repair:' in s for _, _, s in gateway.calls) == 2


def test_verifier_failure_cannot_trigger_question_or_full_author_rewrite(tmp_path, source, candidate):
    class VerifierFailure(RstGateway):
        async def call(self, role, prompt, salt, schema=None, **kwargs):
            self.mode = 'always_false_kill' if self.generations.get(salt.split(':')[0], 0) else 'pass'
            return await super().call(role, prompt, salt, schema, **kwargs)

    pipe, store, _ = setup_rst(tmp_path, source, candidate, rounds=1)
    pipe.gateway = gateway = VerifierFailure(candidate.model_copy(deep=True))
    asyncio.run(pipe.run_curriculum())
    c = store.candidates()[-1]
    assert c.verifier_assessment and not c.trials
    assert all(v.item == c.item for v in c.verifier_assessment.versions)
    assert not any(':rst-question-repair:' in salt for _, _, salt in gateway.calls)


def test_alignment_schema_cannot_add_gold_or_outputs():
    with pytest.raises(ValidationError):
        RstQuestionDraft(question='An otherwise sufficiently long controlled fixture question about a tree.',
            rationale='An otherwise sufficiently long controlled rationale for this fixture question.',
            material_key='fixture-key', required_outputs=['extra proof'], answer='different gold')
