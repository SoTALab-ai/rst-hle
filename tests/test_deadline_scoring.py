import asyncio
import json

import httpx
import pytest
from test_gateway import install_mock
from test_qc import setup_pipeline

from diamond_factory.difficulty import parent_solver_feedback
from diamond_factory.experiment import branch
from diamond_factory.gates import correct_count, sample_outcomes
from diamond_factory.llm import CacheIntegrityError, Gateway
from diamond_factory.pipeline import SampleIncomplete, report


def staged_deadlines(tmp_path, settings, source, candidate, monkeypatch, *, slow=(), failure=None):
    settings.early_screen_samples=4
    settings.solver_deadline_counts_as_incorrect=True
    settings.request_deadline_seconds=.1
    settings.max_retries=0
    pipe,store,_=setup_pipeline(tmp_path,settings,source,candidate)
    pipe.external=[]
    assert asyncio.run(pipe.preflight(candidate))
    calls=[];solver_index=0

    async def handler(request):
        nonlocal solver_index
        payload=json.loads(request.content);calls.append(payload)
        if payload['model']==settings.solver.model:
            i=solver_index;solver_index+=1
            if i in slow:
                await asyncio.sleep(1)
            if failure=='http_500' and i==3:
                return httpx.Response(500)
            text='Final Answer: '+('36' if i<3 else '37')
        else:
            if failure=='judge_deadline':await asyncio.sleep(1)
            yes='Final Answer: 36' in payload['messages'][-1]['content']
            text=json.dumps({'extracted_final_answer':'36' if yes else '37','reasoning':'fixture',
                             'correct':'yes' if yes else 'no','confidence':90,'strict':True})
        return httpx.Response(200,json={'model':payload['model'],'choices':[
            {'message':{'content':text},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':10}})

    install_mock(monkeypatch,handler)
    gateway=Gateway(settings,tmp_path/'receipts',run_id='test')
    pipe.gateway=gateway
    return pipe,store,gateway,calls


def test_prefix_deadline_counts_wrong_triggers_full_sixteen_without_fake_judge(tmp_path,settings,source,candidate,monkeypatch):
    pipe,store,gateway,calls=staged_deadlines(tmp_path,settings,source,candidate,monkeypatch,slow={3,14,15})
    result=asyncio.run(pipe.evaluate(candidate))
    assert len(result.trials)==13 and len(result.deadline_outcomes)==3
    assert correct_count(sample_outcomes(result))==3 and branch(result)=='retained'
    assert sum(p['model']==settings.solver.model for p in calls)==16
    assert sum(p['model']==settings.judge.model for p in calls)==13
    assert all(not {'judgment','response','judge_request'} & d.model_dump().keys() for d in result.deadline_outcomes)
    assert report(store,pipe.quotas)['candidates'][0]['samples']==16
    before=len(calls)
    asyncio.run(pipe.evaluate(result))
    assert len(calls)==before
    for d in result.deadline_outcomes:
        assert d.elapsed_seconds>=d.deadline_seconds
        assert gateway.deadline_outcome(d.sample_id,result.item.question,f'{result.id}:solver:{d.sample_id}',
            d.solver_request,system_prompt=pipe.prompts['solver_hle_system'].strip())==d
    result.item.question+=' changed'
    with pytest.raises(CacheIntegrityError,match='identity mismatch'):
        asyncio.run(pipe.sample(result))
    assert len(calls)==before


def test_all_solver_deadlines_form_zero_of_sixteen_and_feedback_has_no_invented_response(tmp_path,settings,source,candidate,monkeypatch):
    pipe,_,_,calls=staged_deadlines(tmp_path,settings,source,candidate,monkeypatch,slow=set(range(16)))
    result=asyncio.run(pipe.evaluate(candidate))
    assert not result.trials and len(result.deadline_outcomes)==16
    assert branch(result)=='ease' and correct_count(sample_outcomes(result))==0
    assert len(calls)==16
    feedback=parent_solver_feedback(result)
    assert feedback['evaluated_samples']==16 and feedback['paired_samples']==0 and not feedback['examples']
    assert len(feedback['deadline_incorrect_samples'])==16


@pytest.mark.parametrize('failure',['http_500','judge_deadline'])
def test_other_interface_errors_and_judge_deadlines_remain_incomplete(tmp_path,settings,source,candidate,monkeypatch,failure):
    pipe,_,_,_=staged_deadlines(tmp_path,settings,source,candidate,monkeypatch,failure=failure)
    with pytest.raises(SampleIncomplete):asyncio.run(pipe.evaluate(candidate))
    assert not candidate.deadline_outcomes and branch(candidate)=='incomplete'


def test_deadline_receipt_corruption_or_policy_change_cannot_be_scored_on_resume(tmp_path,settings,source,candidate,monkeypatch):
    pipe,_,gateway,calls=staged_deadlines(tmp_path,settings,source,candidate,monkeypatch,slow={3})
    assert asyncio.run(pipe.sample(candidate,4))==3
    outcome=candidate.deadline_outcomes[0]
    before=len(calls)
    settings.solver_deadline_counts_as_incorrect=False
    with pytest.raises(ValueError,match='frozen policy'):asyncio.run(pipe.sample(candidate,4))
    settings.solver_deadline_counts_as_incorrect=True
    path=gateway.directory/(outcome.solver_request+'.json')
    trace=json.loads(path.read_text());trace['elapsed_seconds']=.001;path.write_text(json.dumps(trace))
    with pytest.raises(CacheIntegrityError,match='integrity mismatch'):asyncio.run(pipe.sample(candidate,4))
    assert len(calls)==before


def test_export_revalidates_timeout_receipts_and_includes_their_explicit_outcomes(tmp_path,settings,source,candidate,monkeypatch):
    from diamond_factory import cli
    pipe,store,gateway,calls=staged_deadlines(tmp_path,settings,source,candidate,monkeypatch,slow={3,14,15})
    result=asyncio.run(pipe.evaluate(candidate));result.status='accepted';store.save(result)
    monkeypatch.setattr(cli,'load',lambda _:settings)
    monkeypatch.setattr(cli,'open_store',lambda *a,**kw:store)
    monkeypatch.setattr(cli,'Pipeline',lambda *a,**kw:pipe)
    output=tmp_path/'export.jsonl'
    monkeypatch.setattr('sys.argv',['diamond','export','--output',str(output)])
    before=len(calls);cli.main()
    exported=json.loads(output.read_text())
    assert exported['correct_count']==3 and len(exported['deadline_outcomes'])==3
    assert len(exported['trials'])==13 and len(calls)==before
    deadline=result.deadline_outcomes[0]
    (gateway.directory/(deadline.solver_request+'.json')).unlink()
    blocked=tmp_path/'blocked.jsonl'
    monkeypatch.setattr('sys.argv',['diamond','export','--output',str(blocked)])
    with pytest.raises(CacheIntegrityError,match='Missing deadline receipt'):cli.main()
    assert not blocked.exists() and len(calls)==before
