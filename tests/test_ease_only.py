import asyncio

import httpx
import pytest
from test_experiment import PatternGateway, pricing, staged
from test_gateway import install_mock

from diamond_factory.costs import MeteredGateway
from diamond_factory.experiment import ExperimentPlan, QuestionExperiment, RootPlan, branch
from diamond_factory.pipeline import Pipeline, QualityRejected
from diamond_factory.store import Store


def test_first_four_all_correct_is_terminal_rejection_without_more_calls(tmp_path, settings, source, candidate):
    settings.difficulty_policy='ease_only'
    pipe, _, gateway=staged(tmp_path,settings,source,candidate,set(range(16)))
    result=asyncio.run(pipe.evaluate(candidate))
    assert result.status=='rejected' and branch(result)=='too_easy_rejected'
    assert len(result.trials)==4
    assert sum(role=='solver' for role,_,_ in gateway.calls)==4
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.generate(source,result,{'category':'Math','subtopic':'组合与图论'},1,0))
    assert sum(role=='solver' for role,_,_ in gateway.calls)==4


@pytest.mark.parametrize('pattern,expected', [(set(range(1,16)),'too_easy_rejected'),
    (set(),'ease'),(set(range(1,9)),'retained')])
def test_any_miss_finishes_sixteen_then_rejects_easy_or_eases_zero(tmp_path, settings, source, candidate, pattern, expected):
    settings.difficulty_policy='ease_only'
    pipe,_,gateway=staged(tmp_path,settings,source,candidate,pattern)
    result=asyncio.run(pipe.evaluate(candidate))
    assert len(result.trials)==16 and branch(result)==expected
    assert sum(role=='solver' for role,_,_ in gateway.calls)==16


def test_ease_only_experiment_never_authors_a_harder_child(tmp_path, settings, source, candidate):
    settings.difficulty_policy='ease_only'; settings.early_screen_samples=4
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    root=RootPlan(name='claim',source_id=source.id,category='Math',subtopic='组合与图论',
                  focus='A fixed source result whose first version is too hard and next version too easy')
    plan=ExperimentPlan(roots=[root],max_revisions=2)
    gateway=PatternGateway(candidate,set())
    exp=QuestionExperiment(settings,store,'ease','pilot',tmp_path/'run',gateway,plan=plan,pricing=pricing())
    generations=[]

    async def author(src,parent,target,generation,attempt,brief=None):
        generations.append(generation)
        c=candidate.model_copy(deep=True)
        c.id=exp.generation_identity(src,parent,target,generation,attempt,brief)
        c.root_id=parent.root_id if parent else c.id
        c.parent_id=parent.id if parent else None
        c.run_id='ease';c.generation=generation
        gateway.patterns[c.id]=set() if generation==0 else set(range(16))
        store.save(c);return c

    exp.generate=author
    result=asyncio.run(exp.run_experiment())
    assert generations==[0,1]
    assert [v['decision'] for v in result['roots'][0]['versions']]==['ease','too_easy_rejected']
    assert sum(role=='solver' for role,_,_ in gateway.calls)==20


def test_resume_pins_only_original_bootstrap_identity(tmp_path, settings, source, candidate):
    store=Store(tmp_path/'s.sqlite');store.add_source(source)
    root=RootPlan(name='claim',source_id=source.id,category='Math',subtopic='组合与图论',
                  focus='The exact original frozen root brief must remain unchanged on policy continuation')
    plan=ExperimentPlan(roots=[root],resume_request_run_id='source-run')
    exp=QuestionExperiment(settings,store,'new-run','pilot',tmp_path/'run',PatternGateway(candidate,set()),
                           plan=plan,pricing=pricing())
    target={'category':root.category,'subtopic':root.subtopic}
    expected=Pipeline.generation_identity(type('Identity',(),{'run_id':'source-run'})(),source,None,target,0,0,exp.brief(root,None))
    assert exp.root_identity(root,{source.id:source})==expected
    child=exp.generation_identity(source,candidate,target,1,0,exp.brief(root,candidate))
    old_child=Pipeline.generation_identity(type('Identity',(),{'run_id':'source-run'})(),source,candidate,target,1,0,exp.brief(root,candidate))
    assert child!=old_child


def test_disabled_purchase_caps_keep_http_and_usage_receipts(tmp_path,settings,monkeypatch):
    settings.request_cap_enabled=False;settings.max_requests=1
    calls=[]

    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'model':'glm-5.2','usage':{'prompt_tokens':1000,'completion_tokens':1000},
            'choices':[{'message':{'content':'complete'},'finish_reason':'stop'}]})

    install_mock(monkeypatch,handler)
    rates={'currency':'USD','observed_at':'fixture','rmb_reference_exchange_rate':7,
        'rates_per_million':{getattr(settings,r).model:{'input':1,'output':2,'cache_read':.1,'cache_write':.5}
                            for r in ('generator','auditor','consistency','solver','judge')}}
    gateway=MeteredGateway(settings,tmp_path,run_id='unlimited',pricing=rates,visible_cost_limit_usd=None)

    async def run():
        for i in range(3):
            await gateway.call('solver','Same question','sample:'+str(i))
    asyncio.run(run())
    assert len(calls)==3 and len(list((tmp_path/'attempts').rglob('*.json')))==3
    assert ExperimentPlan(roots=[RootPlan(name='r',source_id='s',category='Math',subtopic='分析',
        focus='A synthetic fixed source planning fixture only.')],visible_cost_limit_usd=None).visible_cost_limit_usd is None


def test_pending_saved_question_resumes_quality_checks_without_rebuying_author(tmp_path,settings,source,candidate):
    from test_qc import setup_pipeline
    pipe,store,gateway=setup_pipeline(tmp_path,settings,source,candidate)
    target={'category':'Math','subtopic':'组合与图论'}
    candidate.id=pipe.generation_identity(source,None,target,0,0)
    candidate.root_id=candidate.id;candidate.authoring_complete=False
    store.save(candidate)
    result=asyncio.run(pipe.generate(source,None,target,0,0))
    assert result.authoring_complete and result.solution==candidate.solution
    assert all(role=='consistency' for role,_,_ in gateway.calls)


def test_last_pending_quality_failure_does_not_restart_author_repair_budget(tmp_path,settings,source,candidate):
    from test_qc import setup_pipeline
    pipe,store,gateway=setup_pipeline(tmp_path,settings,source,candidate)
    target={'category':'Math','subtopic':'组合与图论'}
    candidate.id=pipe.generation_identity(source,None,target,0,0)
    candidate.root_id=candidate.id;candidate.authoring_complete=False
    candidate.authoring_repairs_completed=settings.max_repairs
    store.save(candidate)

    async def fail(c):
        c.status='rejected';c.reasons=['controlled_quality_failure'];return False
    pipe.preflight=fail
    result=asyncio.run(pipe.generate(source,None,target,0,0))
    assert result.status=='rejected' and result.authoring_complete and not gateway.calls
