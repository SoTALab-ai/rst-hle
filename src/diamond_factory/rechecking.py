"""在新快照中只重判既有答卷，验证旧solver收据，禁止任何出题或解题调用。"""
import json

from pydantic import Field

from .costs import MeteredGateway, measure_cost
from .evaluation import judge_answer
from .experiment import QuestionExperiment, golden_context
from .gates import correct_count, sample_outcomes
from .models import Audit, Judgment, StrictModel, digest
from .runtime import atomic_json, gather_cancel_on_error


class RecheckOnlyGateway(MeteredGateway):
    async def call(self, role, *args, **kwargs):
        if role not in ('judge', 'auditor', 'consistency'):
            raise ValueError('Existing-answer recheck forbids solver and author calls')
        return await super().call(role, *args, **kwargs)


def immutable_answer_input(c):
    return {'item':c.item.model_dump(),'solution':c.solution.model_dump(),
            'answers':[{'sample_id':t.sample_id,'response':t.response,'solver_request':t.solver_request,
                        'solver_actual_model':t.solver_actual_model} for t in c.trials],
            'deadlines':[d.model_dump() for d in c.deadline_outcomes]}


class RejudgeChange(StrictModel):
    sample_id: int
    solver_request: str
    response_sha256: str
    old_judge_request: str
    old_judgment: Judgment
    new_judge_request: str
    new_judgment: Judgment
    new_actual_model: str


class AnswerRecheckReport(StrictModel):
    run_id: str
    source_run_id: str
    candidate_id: str
    immutable_input_sha256: str
    source_candidate_sha256: str
    answered_samples: int
    deadline_incorrect_samples: int
    old_correct: int
    new_correct: int
    changed_samples: list[int]
    changes: list[RejudgeChange]
    question_audit: Audit
    question_audit_request: str
    post_audit: Audit | None
    retained: bool
    scientific_and_grading_review_passed: bool
    known_cost_usd: float
    unknown_usage_attempts: int
    http_attempts: int
    solver_http_attempts: int = Field(default=0, ge=0, le=0)


class ExistingAnswerRecheck(QuestionExperiment):
    async def recheck(self, original, *, expected_source_candidate_sha256):
        if original.run_id == self.run_id:
            raise ValueError('Rechecking requires a new run, never overwrite the original judgment')
        if not isinstance(self.gateway, RecheckOnlyGateway):
            raise TypeError('An existing-answer recheck requires a gateway that forbids solver calls')
        c=original.model_copy(deep=True)
        source_run=c.run_id;source_sha=digest(c.model_dump())
        if source_sha!=expected_source_candidate_sha256:
            raise ValueError('Source candidate differs from the frozen source snapshot')
        frozen=digest(immutable_answer_input(c))
        old_correct=correct_count(sample_outcomes(c))
        # 先验收所有已付费答卷和截止收据，再允许任何新judge请求。
        for t in c.trials:
            self.gateway.verify_saved_answer(c.item.question,f'{c.id}:solver:{t.sample_id}',
                t.solver_request,t.response,t.solver_actual_model,
                system_prompt=self.prompts['solver_hle_system'].strip())
        self.validate_deadlines(c)
        self.start({'command':'recheck-existing-answers','source_run':source_run,
                    'source_candidate_sha256':source_sha,'immutable_input_sha256':frozen,
                    'plan_sha256':digest(self.plan.model_dump()),'pricing_sha256':digest(self.pricing)})
        c.run_id=self.run_id
        changes=[]
        question_audit=None
        post=None
        try:
            async def one(t):
                new,rid,model=await judge_answer(self.gateway,self.prompts['judge'].format(
                    question=c.item.question,correct_answer=c.item.answer,response=t.response),
                    f'{c.id}:recheck:{self.run_id}:judge:{t.sample_id}',self.settings.judge_schema_repairs,
                    system_prompt=self.prompts.get('judge_system'))
                return RejudgeChange(sample_id=t.sample_id,solver_request=t.solver_request,
                    response_sha256=digest(t.response),old_judge_request=t.judge_request,
                    old_judgment=t.judgment,new_judge_request=rid,new_judgment=new,new_actual_model=model)

            changes=await gather_cancel_on_error(*(one(t) for t in c.trials))
            by_id={d.sample_id:d for d in changes}
            c.trials=[t.model_copy(update={'judgment':by_id[t.sample_id].new_judgment,
                'judge_request':by_id[t.sample_id].new_judge_request,
                'judge_actual_model':by_id[t.sample_id].new_actual_model}) for t in c.trials]
            if digest(immutable_answer_input(c))!=frozen:
                raise ValueError('Question, gold, source answer or deadline changed during rechecking')
            question_audit,qaid,_=await self.gateway.call('auditor',self.prompts['recheck_question']+'\n'+json.dumps({
                'item':c.item.model_dump(),'initial_golden':golden_context(c),
                'sources':[{**s.model_dump(),'text':s.text[:self.settings.max_source_chars]}
                           for s in self.store.sources() if s.id in c.source_ids]},ensure_ascii=False),
                f'{c.id}:recheck:{self.run_id}:question-audit',Audit)
            c.audit=question_audit
            c.status='quality_seed' if question_audit.passed() else 'rejected'
            c.reasons=[] if question_audit.passed() else ['recheck_question_failed:'+question_audit.reason]
            self.store.save(c)
            if question_audit.passed() and await self.preflight(c):
                await self.final_audit(c)
                post=c.audit
                if post.passed():
                    await self.evaluate(c)
            self.metadata('completed' if c.status=='screened' else 'completed_with_errors')
            self.snapshot()
        except BaseException:
            self.metadata('interrupted')
            raise
        cost=measure_cost(self.store,self.run_id,self.run_dir/'requests',self.settings,self.pricing)
        if cost['by_role'].get('solver',{}).get('http_attempts',0):
            raise ValueError('Forbidden solver HTTP attempt in recheck ledger')
        result=AnswerRecheckReport(run_id=self.run_id,source_run_id=source_run,candidate_id=c.id,
            immutable_input_sha256=frozen,source_candidate_sha256=source_sha,answered_samples=len(c.trials),
            deadline_incorrect_samples=len(c.deadline_outcomes),old_correct=old_correct,
            new_correct=correct_count(sample_outcomes(c)),changes=changes,
            changed_samples=[d.sample_id for d in changes if d.old_judgment.correct!=d.new_judgment.correct],
            question_audit=question_audit,question_audit_request=qaid,post_audit=post,
            retained=c.status in ('screened','accepted'),
            scientific_and_grading_review_passed=bool(question_audit.passed() and post and post.passed()),
            known_cost_usd=cost['known_cost_usd'],unknown_usage_attempts=cost['unknown_usage_attempts'],
            http_attempts=cost['http_attempts'])
        atomic_json(self.run_dir/'answer-recheck-report.json',result.model_dump())
        return result
