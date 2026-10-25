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
                result=result, proof=proof)
        elif schema is RstGrowthAudit:
            value = RstGrowthAudit(parent_core_preserved=True, necessary_new_dependency=self.growth,
                substantive_change=self.growth, compact_target=True, parent_proof_entries=[1],
                child_proof_entries=[2], shortest_correct_route=['Controlled equivalent fixture route.'],
                reason='Controlled growth decision used to test lineage and bounds only.')
        elif schema is RstQuestionDraft:
            value = RstQuestionDraft(question=self.candidate.item.question + ' Controlled version ' + salt[:24] + '.',
                rationale=self.candidate.item.rationale, material_key='controlled-rst-version', required_outputs=['edge count'])
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
