import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import trials
from test_gateway import install_mock
from test_qc import setup_pipeline

from diamond_factory.evaluation import judge_answer
from diamond_factory.experiment import ExperimentPlan, RootPlan
from diamond_factory.llm import CacheIntegrityError, Gateway, ModelResponseError
from diamond_factory.models import DeadlineOutcome, Judgment, digest
from diamond_factory.rechecking import ExistingAnswerRecheck, RecheckOnlyGateway, immutable_answer_input
from diamond_factory.store import Store


def rates(settings):
    return {'currency':'USD','observed_at':'fixture','rmb_reference_exchange_rate':7.0,
        'rates_per_million':{getattr(settings,r).model:{'input':1,'output':2,'cache_read':.1,'cache_write':.5}
                            for r in ('generator','auditor','consistency','solver','judge')}}


def test_context_extraction_guidance_and_official_prompt_do_not_change_on_format_repair():
    system=Path('prompts/judge_system.txt').read_text()
    prompt=Path('prompts/judge.txt').read_text().format(question='Use stripped F.',correct_answer='B',
        response='Box: Theta B. If F is stripped, omit Theta.')
    calls=[]

    class Judge:
        async def call(self,role,p,salt,schema,**kwargs):
            calls.append((role,p,salt,kwargs))
            if len(calls)==1:raise ModelResponseError('judge','format','invalid_schema')
            return Judgment(extracted_final_answer='B',reasoning='Applicable explicit condition.',
                            correct='no',confidence=80,strict=True),'new','gpt-6-luna'
    result=asyncio.run(judge_answer(Judge(),prompt,'s',system_prompt=system))
    assert result[0].correct=='no' and len(calls)==2
    assert all(c[1]==prompt and c[3]=={'system_prompt':system} for c in calls)
    assert len({c[2] for c in calls})==2
    contract=json.loads(Path('configs/judge-contract.json').read_text())
    assert contract['prompt_sha256']==digest(Path('prompts/judge.txt').read_text())
    assert contract['system_prompt_sha256']==digest(system)


def test_saved_answer_is_verified_without_resampling_and_tampering_is_rejected(tmp_path,settings,monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'model':settings.solver.model,'choices':[
            {'message':{'content':'unchanged answer'},'finish_reason':'stop'}]})
    install_mock(monkeypatch,handler)
    gateway=Gateway(settings,tmp_path)
    response,rid,actual=asyncio.run(gateway.call('solver','Q','c:solver:0',system_prompt='solver system'))
    seal=gateway.verify_saved_answer('Q','c:solver:0',rid,response,actual,system_prompt='solver system')
    assert seal and len(calls)==1
    with pytest.raises(CacheIntegrityError):
        gateway.verify_saved_answer('Q changed','c:solver:0',rid,response,actual,system_prompt='solver system')
    with pytest.raises(CacheIntegrityError):
        gateway.verify_saved_answer('Q','c:solver:0',rid,'edited answer',actual,system_prompt='solver system')
    path=tmp_path/(rid+'.json');data=json.loads(path.read_text());data['content']='tampered'
    path.write_text(json.dumps(data))
    with pytest.raises(CacheIntegrityError):
        gateway.verify_saved_answer('Q','c:solver:0',rid,response,actual,system_prompt='solver system')
    assert len(calls)==1


@pytest.mark.parametrize('role',['solver','generator'])
def test_existing_answer_gateway_forbids_solver_and_author(tmp_path,settings,role):
    gateway=RecheckOnlyGateway(settings,tmp_path,pricing=rates(settings),visible_cost_limit_usd=None)
    with pytest.raises(ValueError,match='forbids'):
        asyncio.run(gateway.call(role,'must not be submitted','s'))
    assert gateway.attempts==0


@pytest.mark.parametrize('question_valid',[True,False])
def test_recheck_preserves_source_answers_and_deadlines_and_separates_question_gate(
        tmp_path,settings,source,candidate,question_valid):
    settings.solver_deadline_counts_as_incorrect=True
    oldpipe,_,_=setup_pipeline(tmp_path/'source',settings,source,candidate)
    assert asyncio.run(oldpipe.preflight(candidate))
    candidate.trials=[t for t in trials(3) if t.sample_id not in (8,10,12)]
    candidate.deadline_outcomes=[DeadlineOutcome(sample_id=i,solver_request='deadline-'+str(i),
        requested_model=settings.solver.model,deadline_seconds=600.0,elapsed_seconds=600.1) for i in (8,10,12)]
    original=candidate.model_copy(deep=True);old_sha=digest(original.model_dump())
    source_answers={t.sample_id:t for t in original.trials};source_deadlines={d.sample_id:d for d in original.deadline_outcomes}
    calls=[];verified=[]

    class Replayer(RecheckOnlyGateway):
        def verify_saved_answer(self,question,salt,rid,response,actual_model,**kwargs):
            t=source_answers[int(salt.split(':')[-1])]
            assert (question,rid,response,actual_model)==(original.item.question,t.solver_request,t.response,t.solver_actual_model)
            verified.append(t.sample_id);return digest(response)

        def deadline_outcome(self,i,question,salt,rid,**kwargs):
            assert rid==source_deadlines[i].solver_request
            return source_deadlines[i]

        async def call(self,role,prompt,salt,schema=None,**kwargs):
            assert role not in ('solver','generator');calls.append((role,prompt,salt))
            if role=='judge':
                i=int(salt.split(':')[-1]);old=source_answers[i].judgment
                grade=old.model_copy(update={'correct':'yes'}) if i==3 else old
                return grade,digest(salt),settings.judge.model
            if ':question-audit' in salt:
                packet=json.loads(prompt.splitlines()[-1])
                assert packet['initial_golden']['reference_solution']['answer']==original.solution.answer
                audit=original.audit.model_copy(update={'valid':question_valid,'reason':'question gate fixture'})
                return audit,digest(salt),settings.auditor.model
            assert ':post-evaluation-audit-v3' in salt
            packet=json.loads(prompt.splitlines()[-1])
            assert {r['sample_id'] for r in packet['graded_responses']}==set(source_answers)
            return original.audit,digest(salt),settings.auditor.model

    store=Store(tmp_path/'new.sqlite');store.add_source(source)
    gateway=Replayer(settings,tmp_path/'new/requests',pricing=rates(settings),visible_cost_limit_usd=None)
    plan=ExperimentPlan(roots=[RootPlan(name='r',source_id=source.id,category='Math',subtopic='组合与图论',
        focus='Controlled independent receipt-only rechecking fixture.')])
    exp=ExistingAnswerRecheck(settings,store,'new','pilot',tmp_path/'new',gateway,plan=plan,pricing=rates(settings))
    exp.external=[]
    result=asyncio.run(exp.recheck(original,expected_source_candidate_sha256=old_sha))
    assert result.old_correct==3 and result.new_correct==4 and result.changed_samples==[3]
    assert result.deadline_incorrect_samples==3 and result.answered_samples==13
    assert set(verified)==set(source_answers) and sum(r=='judge' for r,_,_ in calls)==13
    assert result.retained==question_valid and result.scientific_and_grading_review_passed==question_valid
    assert digest(original.model_dump())==old_sha
    assert immutable_answer_input(store.candidates()[0])==immutable_answer_input(original)
    with pytest.raises(ValueError,match='frozen source snapshot'):
        asyncio.run(exp.recheck(original,expected_source_candidate_sha256='bad'))
