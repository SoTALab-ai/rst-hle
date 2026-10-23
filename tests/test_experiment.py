import asyncio
import json

import pytest
from test_qc import FakeGateway, setup_pipeline

from diamond_factory.catalog import Paper, catalog_rows, save_catalog
from diamond_factory.costs import measure_cost, price_usage, validate_pricing
from diamond_factory.experiment import ExperimentPlan, QuestionExperiment, RootPlan, branch
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import Source, digest
from diamond_factory.pipeline import SampleIncomplete
from diamond_factory.store import Store


class PatternGateway(FakeGateway):
    def __init__(self,candidate,correct_samples,fail_sample=None):
        super().__init__(candidate)
        self.correct_samples = correct_samples
        self.fail_at = fail_sample
        self.patterns = {}

    async def call(self,role,prompt,salt,schema=None,**kwargs):
        if role == "solver":
            self.calls.append((role,prompt,salt))
            n = int(salt.split(":")[-1])
            if n == self.fail_at:
                raise ModelResponseError(role,digest(salt),"deadline_exceeded")
            pattern = self.patterns.get(salt.split(":")[0],self.correct_samples)
            return "Final Answer: "+("36" if n in pattern else "37"),digest(salt),"glm-5.2"
        return await super().call(role,prompt,salt,schema,**kwargs)


def staged(tmp_path,settings,source,candidate,pattern,fail=None):
    settings.early_screen_samples = 4
    pipe,store,_ = setup_pipeline(tmp_path,settings,source,candidate)
    pipe.gateway = PatternGateway(candidate,pattern,fail)
    return pipe,store,pipe.gateway


def test_four_correct_is_only_preliminary_and_never_sixteen_acceptance(tmp_path,settings,source,candidate):
    pipe,_,gateway = staged(tmp_path,settings,source,candidate,set(range(16)))
    c = asyncio.run(pipe.evaluate(candidate))
    assert len(c.trials) == 4 and c.status == "quality_seed"
    assert branch(c) == "preliminary_harden"
    assert sum(role=="solver" for role,_,_ in gateway.calls) == 4
    with pytest.raises(ValueError,match="Incomplete"):
        __import__('diamond_factory.gates',fromlist=['correct_count']).correct_count(c.trials)


@pytest.mark.parametrize("pattern,expected",[(set(range(1,9)),"retained"),(set(range(1,16)),"harden"),
                                           (set(),"ease"),({8,9,10,11},"retained")])
def test_any_valid_miss_requires_all_sixteen_even_zero_of_first_four(tmp_path,settings,source,candidate,pattern,expected):
    pipe,_,gateway = staged(tmp_path,settings,source,candidate,pattern)
    c = asyncio.run(pipe.evaluate(candidate))
    assert len(c.trials) == 16 and branch(c) == expected
    assert sum(role=="solver" for role,_,_ in gateway.calls) == 16


def test_screen_timeout_is_not_a_wrong_answer_and_cannot_branch(tmp_path,settings,source,candidate):
    pipe,_,_ = staged(tmp_path,settings,source,candidate,set(range(16)),fail=2)
    with pytest.raises(SampleIncomplete):
        asyncio.run(pipe.evaluate(candidate))
    assert len(candidate.trials) == 3 and branch(candidate) == "incomplete"


def test_prefix_samples_are_reused_when_finishing_sixteen(tmp_path,settings,source,candidate):
    pipe,_,gateway = staged(tmp_path,settings,source,candidate,{0,1,2,4,5})
    assert asyncio.run(pipe.sample(candidate,4)) == 3
    before = {t.solver_request for t in candidate.trials}
    asyncio.run(pipe.evaluate(candidate))
    assert before <= {t.solver_request for t in candidate.trials}
    assert sum(role=="solver" for role,_,_ in gateway.calls) == 16


def pricing():
    return {"observed_at":"test-fixture","rmb_reference_exchange_rate":7,
            "rates_per_million":{"glm-5.2":{"input":1,"output":2,"cache_read":.1,"cache_write":.5}}}


def test_usage_cost_prices_cache_and_leaves_missing_usage_unknown():
    usage = {"prompt_tokens":1000,"completion_tokens":2000,
             "prompt_tokens_details":{"cached_tokens":100,"cache_write_tokens":200}}
    assert price_usage("glm-5.2",usage,pricing()) == pytest.approx(.00481)
    assert price_usage("glm-5.2",{"completion_tokens":100},pricing()) is None


def test_missing_or_unverified_prices_fail_before_any_model_purchase(settings):
    with pytest.raises(ValueError,match="dated USD"):
        validate_pricing(settings,pricing())
    p=dict(pricing(),currency="USD")
    with pytest.raises(ValueError,match="Missing verified"):
        validate_pricing(settings,p)


def test_corrupt_math_escape_is_rejected_instead_of_silently_repaired(candidate,source,quotas):
    from diamond_factory.gates import static_errors
    candidate.item.question+=' Evaluate \x0crac{1}{2} and \right.'
    assert 'invalid_control_character_in_math_text' in static_errors(candidate,{source.id:source},quotas)


def test_cost_counts_paid_truncation_and_unindexed_cancellation_once(tmp_path,settings):
    directory = tmp_path/'requests';directory.mkdir()
    paid = {"request_id":"paid","ordinal":1,"run_id":"r","role":"solver",
            "usage":{"prompt_tokens":1000,"completion_tokens":1000},"status_code":200}
    cancelled = {"request_id":"cancelled","ordinal":2,"run_id":"r","role":"solver",
                 "usage":{},"error":"cancelled_in_flight"}
    (directory/'paid.json').write_text(json.dumps({"request_id":"paid","salt":"candidate:solver:0",
        "response_error":"length","attempts":[paid]}))
    for receipt in (paid,cancelled):
        p=directory/'attempts'/receipt['request_id']/'1.json';p.parent.mkdir(parents=True)
        p.write_text(json.dumps(receipt))
    result = measure_cost(Store(tmp_path/'s.sqlite'),'r',directory,settings,pricing())
    assert result['http_attempts']==2 and result['unknown_usage_attempts']==1
    assert result['known_cost_usd']==pytest.approx(.003)


def test_multiple_roots_from_same_paper_remain_distinct(tmp_path,settings,source,candidate):
    settings.early_screen_samples=4
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    roots=[RootPlan(name='claim'+str(i),source_id=source.id,category='Math',subtopic='组合与图论',
                    focus='An independent claim with a different reasoning structure '+str(i)) for i in range(2)]
    plan=ExperimentPlan(roots=roots,max_revisions=0)
    gateway=PatternGateway(candidate,set(range(16)))
    experiment=QuestionExperiment(settings,store,'r','pilot',tmp_path/'r',gateway,plan=plan,pricing=pricing())

    async def author(src,parent,target,generation,attempt,brief=None):
        c=candidate.model_copy(deep=True)
        c.id=experiment.generation_identity(src,parent,target,generation,attempt,brief)
        c.root_id=c.id;c.run_id='r';store.save(c);return c

    experiment.generate=author
    result=asyncio.run(experiment.run_experiment())
    assert result['generated_versions']==2 and result['roots_with_generated_question']==2
    ids=[r['versions'][0]['id'] for r in result['roots']]
    assert len(set(ids))==2
    assert result['preliminary_4_of_4_versions']==2 and result['roots_with_retained_question']==0


def test_experiment_exercises_hard_to_easy_and_easy_to_hard_with_one_root(tmp_path,settings,source,candidate):
    settings.early_screen_samples=4
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    root=RootPlan(name='claim',source_id=source.id,category='Math',subtopic='组合与图论',
                  focus='Use three successive versions with a fixed underlying mathematical claim')
    plan=ExperimentPlan(roots=[root],max_revisions=2)
    gateway=PatternGateway(candidate,set())
    experiment=QuestionExperiment(settings,store,'r','pilot',tmp_path/'r',gateway,plan=plan,pricing=pricing())

    async def author(src,parent,target,generation,attempt,brief=None):
        identity=experiment.generation_identity(src,parent,target,generation,attempt,brief)
        existing=next((c for c in store.candidates() if c.id==identity),None)
        if existing:return existing
        c=candidate.model_copy(deep=True);c.id=identity;c.run_id='r';c.generation=generation
        c.parent_id=parent.id if parent else None;c.root_id=parent.root_id if parent else identity
        gateway.patterns[identity]=[set(),set(range(16)),{0,1,2,4}][generation]
        store.save(c);return c

    experiment.generate=author
    result=asyncio.run(experiment.run_experiment())
    assert result['generated_versions']==3 and result['roots_with_retained_question']==1
    assert result['first_generation_retained']==0 and result['bounded_iteration_yield']==1
    assert [v['decision'] for v in result['roots'][0]['versions']]==['ease','preliminary_harden','retained']
    assert sum(role=='solver' for role,_,_ in gateway.calls)==36
    calls=len(gateway.calls)
    asyncio.run(experiment.run_experiment())
    assert len(gateway.calls)==calls


def test_catalog_requires_full_text_and_verifies_metadata_hash(tmp_path):
    store=Store(tmp_path/'s.sqlite')
    p=Paper(source_id='arxiv:1304.4132v2',arxiv_id='1304.4132v2',title='Synthetic publication fixture',
        authors=['Fixture author'],domain='Math',subtopic='组合与图论',journal='Fixture journal',
        publication_year=2020,doi='10.1000/fixture',publisher_url='https://example.org/paper',
        venue_evidence_url='https://example.org/metadata',arxiv_categories=['math.CO'],
        selection_reason='Synthetic metadata used exclusively for tests.').sealed()
    with pytest.raises(ValueError,match='full-text'):
        save_catalog(store,[p])
    source=Source(id=p.source_id,title=p.title,uri='synthetic://paper',kind='synthetic',text='fixture '*40)
    store.add_source(source);save_catalog(store,[p])
    assert catalog_rows(store,'Math')[0]['doi']==p.doi and catalog_rows(store,'Physics')==[]
    with pytest.raises(ValueError,match='hash mismatch'):
        p.model_copy(update={'doi':'10.1000/changed'}).sealed()


def test_assessment_defect_found_after_sampling_prevents_retention_and_descendants(tmp_path,settings,source,candidate):
    settings.early_screen_samples=4
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    root=RootPlan(name='claim',source_id=source.id,category='Math',subtopic='组合与图论',
                  focus='A controlled assessment whose post-audit discovers an ambiguous convention')
    plan=ExperimentPlan(roots=[root],max_revisions=2)

    class DefectGateway(PatternGateway):
        async def call(self,role,prompt,salt,schema=None,**kwargs):
            if ':post-evaluation-audit-v2' in salt:
                self.calls.append((role,prompt,salt))
                payload=json.loads(prompt[prompt.index('\n')+1:])
                examples=payload['allegedly_incorrect_responses']
                assert len(examples)==4 and all(x['response']=='Final Answer: 37' for x in examples)
                audit=candidate.audit.model_copy(update={'valid':False,
                    'reason':'Controlled counterexample: the question leaves the ratio convention undefined.'})
                return audit,digest(salt),'fixture-model'
            return await super().call(role,prompt,salt,schema,**kwargs)

    gateway=DefectGateway(candidate,{0,2,4,6})
    experiment=QuestionExperiment(settings,store,'r','pilot',tmp_path/'r',gateway,plan=plan,pricing=pricing())

    async def author(src,parent,target,generation,attempt,brief=None):
        c=candidate.model_copy(deep=True);c.id=experiment.root_identity(root,{source.id:source})
        c.root_id=c.id;c.run_id='r';store.save(c);return c

    experiment.generate=author
    result=asyncio.run(experiment.run_experiment())
    assert result['roots_with_retained_question']==0 and result['generated_versions']==1
    version=result['roots'][0]['versions'][0]
    assert version['samples']==16 and version['observed_correct']==4
    assert version['decision']=='quality_rejected' and not result['transitions']


def test_failed_rewrite_without_a_saved_candidate_is_reported_as_completed_with_errors(tmp_path,settings,source,candidate):
    settings.early_screen_samples=4
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    root=RootPlan(name='claim',source_id=source.id,category='Math',subtopic='组合与图论',
                  focus='A controlled rewrite that fails its author response schema')
    experiment=QuestionExperiment(settings,store,'r','pilot',tmp_path/'r',PatternGateway(candidate,set(range(16))),
                                  plan=ExperimentPlan(roots=[root],max_revisions=2),pricing=pricing())

    async def author(src,parent,target,generation,attempt,brief=None):
        if generation:raise ModelResponseError('generator','fixture-request','invalid_schema')
        c=candidate.model_copy(deep=True);c.id=experiment.root_identity(root,{source.id:source})
        c.root_id=c.id;c.run_id='r';store.save(c);return c

    experiment.generate=author
    result=asyncio.run(experiment.run_experiment())
    assert result['generated_versions']==1 and result['roots_with_retained_question']==0
    assert result['roots'][0]['state']['phase']=='prefix_failed'
    assert store.runs()['r']['status']=='completed_with_errors'


def test_author_repair_resumes_after_a_saved_rejection_without_repeating_first_draft(tmp_path,settings,source,candidate):
    from diamond_factory.models import Audit, Question, Solution
    pipe,store,_=setup_pipeline(tmp_path,settings,source,candidate)

    class Author:
        def __init__(self,interrupt):
            self.calls=[];self.interrupt=interrupt

        async def call(self,role,prompt,salt,schema,**_kw):
            self.calls.append(salt)
            if self.interrupt and ':repair:1:solution' in salt:
                raise OSError('simulated shutdown between author repairs')
            if schema is Solution:answer=candidate.solution.model_copy(deep=True)
            elif schema is Audit:answer=candidate.audit.model_copy(deep=True)
            elif schema is Question:
                answer=candidate.item.model_copy(deep=True)
                if ':repair:0:' in salt:answer.evidence[0].quote='This claim is not in the synthetic source.'
            else:raise AssertionError(schema)
            return answer,digest(salt),'fixture-model'

    pipe.gateway=Author(True)
    target={'category':'Math','subtopic':'组合与图论'}
    with pytest.raises(OSError,match='shutdown'):
        asyncio.run(pipe.generate(source,None,target,0,0))
    saved=store.candidates()[0]
    assert saved.status=='rejected' and not saved.authoring_complete
    assert saved.authoring_repairs_completed==1
    pipe.gateway=Author(False)
    fixed=asyncio.run(pipe.generate(source,None,target,0,0))
    assert fixed.authoring_complete and fixed.status=='quality_seed'
    assert len(pipe.gateway.calls)==3 and all(':repair:1:' in s for s in pipe.gateway.calls)
