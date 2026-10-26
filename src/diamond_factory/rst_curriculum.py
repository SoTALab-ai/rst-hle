"""独立RST研究协议：科学有效种子逐轮扩展，绝对难度由GLM实测。"""
import asyncio
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .artifacts import LocalObjects
from .consistency import QualityRejected
from .costs import measure_cost
from .experiment import QuestionExperiment
from .gates import sample_outcomes
from .llm import BudgetExceeded, CacheIntegrityError, ModelResponseError
from .models import (
    Audit,
    Candidate,
    KnownResult,
    LockedProofDraft,
    Question,
    RstGrowthAudit,
    RstNonRegressionAudit,
    RstNonRegressionEvidence,
    RstOrigin,
    RstParentSnapshot,
    StepwiseBlueprintAudit,
    StrictModel,
    digest,
)
from .pipeline import Pipeline, SampleIncomplete
from .rst_difficulty import (
    proposal_contract,
    question_contract,
    regression_errors,
    review_non_regression,
    valid_non_regression,
)
from .runtime import atomic_json, gather_cancel_on_error

OPERATORS = ("latent_representation", "coupled_compatibility", "boundary_observable")


def effective_prompts(prompts):
    return {**prompts, **{key: prompts['rst_' + replacement] for key, replacement in (
        ('blueprint_audit', 'blueprint_audit'), ('audit', 'question_audit'), ('bottleneck_audit', 'mask_audit'))}}


def rst_protocol(settings, prompts):
    return digest({"protocol": 'rst-curriculum-v2-non-regression', "settings": settings.model_dump(),
        "prompts": effective_prompts(prompts), "code": Path(__file__).read_text(),
        "burden_code": Path(__file__).with_name('rst_difficulty.py').read_text(),
        "models_code": Path(__file__).with_name('models.py').read_text(),
        "endpoints": {role: digest(os.environ.get(getattr(settings, role).base_url_env, '').rstrip('/'))
                   for role in ('generator', 'auditor', 'consistency')}})


def contract(candidate):
    return digest({k: candidate.model_dump()[k] for k in (
        'id', 'parent_id', 'root_id', 'generation', 'source_ids', 'source_fidelity', 'solution', 'item',
        'known_result', 'blueprint_audit', 'blueprint_request', 'blueprint_fingerprint')})


def parent_snapshot(candidate):
    return RstParentSnapshot(candidate_id=candidate.id, root_id=candidate.root_id,
        generation=candidate.generation, source_ids=candidate.source_ids, item=candidate.item,
        solution=candidate.solution, known_result=candidate.known_result)


def valid_rst_origin(candidate, sources, expected_protocol):
    origin = candidate.rst_origin
    if (not origin or origin.protocol_sha256 != expected_protocol
            or origin.contract_sha256 != contract(candidate) or len(candidate.source_ids) != 1
            or candidate.source_ids[0] not in sources
            or origin.source_sha256 != sources[candidate.source_ids[0]].sha256):
        return False
    if candidate.generation == 0:
        return bool(candidate.parent_id is None and origin.imported_seed_sha256
                    and origin.parent_snapshot is None and origin.growth_audit is None)
    parent, review = origin.parent_snapshot, origin.growth_audit
    return bool(parent and review and review.passed() and valid_non_regression(candidate) and origin.growth_audit_request
        and origin.proposal_request and origin.alignment_request
        and origin.parent_contract_sha256 == digest(parent.model_dump())
        and parent.candidate_id == candidate.parent_id and parent.root_id == candidate.root_id
        and parent.generation + 1 == candidate.generation and parent.source_ids == candidate.source_ids
        and max(review.parent_proof_entries) <= len(parent.solution.derivation)
        and max(review.child_proof_entries) <= len(candidate.solution.derivation))


class RstProposal(StrictModel):
    outcome: Literal['proposed', 'unavailable'] = 'proposed'
    reason: str = Field(min_length=20)
    subtopic: str
    extension: str = Field(min_length=30)
    required_outputs: list[str] = Field(default_factory=list)
    result: KnownResult | None
    proof: LockedProofDraft | None

    @model_validator(mode='after')
    def coherent(self):
        if (self.outcome == 'proposed') != bool(self.result and self.proof):
            raise ValueError('Proposed needs both result and proof; unavailable needs neither')
        if self.outcome == 'unavailable' and (self.result or self.proof):
            raise ValueError('Unavailable must not invent a partial result')
        if self.outcome == 'proposed' and not self.required_outputs:
            raise ValueError('A proposed target must lock its required outputs before alignment')
        return self


class RstQuestionDraft(StrictModel):
    question: str = Field(min_length=50)
    rationale: str = Field(min_length=50)
    material_key: str = Field(min_length=5)


class RstQuestionRepair(StrictModel):
    outcome: Literal['question_contract_only', 'proof_or_gold_defect', 'uncertain']
    reason: str = Field(min_length=20)
    revised: RstQuestionDraft | None

    @model_validator(mode='after')
    def bounded_scope(self):
        if (self.outcome == 'question_contract_only') != (self.revised is not None):
            raise ValueError('Only a question-contract repair may emit a revised question')
        return self


class RstSeed(StrictModel):
    name: str = Field(pattern=r'^[A-Za-z0-9_-]{1,50}$')
    candidate: Candidate
    sha256: str

    @model_validator(mode='after')
    def sealed(self):
        if self.sha256 != digest(self.candidate.model_dump()):
            raise ValueError('Seed changed; choose a new frozen plan')
        if self.candidate.item.category not in ('Math', 'Physics') or not self.candidate.known_result:
            raise ValueError('RST theory seeds need a locked result and Math/Physics category')
        return self


class RstPlan(StrictModel):
    seeds: list[RstSeed] = Field(min_length=1, max_length=10)
    growth_rounds: int = Field(default=3, ge=1, le=15)
    proposal_attempts: int = Field(default=3, ge=1, le=3)
    root_workers: int = Field(default=2, ge=1, le=4)
    visible_cost_limit_usd: float | None = Field(default=None, gt=0)
    reuse_manifest_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def unique_names(self):
        if len({s.name for s in self.seeds}) != len(self.seeds):
            raise ValueError('Duplicate lineage names')
        return self


class RstAttempt(StrictModel):
    candidate_id: str
    proposal: RstProposal | None = None
    proposal_request: str = ''
    blueprint: StepwiseBlueprintAudit | None = None
    growth: RstGrowthAudit | None = None
    non_regression: RstNonRegressionAudit | None = None
    non_regression_request: str = ''
    non_regression_actual_model: str = ''
    errors: list[str] = Field(default_factory=list)
    question_versions: list[Candidate] = Field(default_factory=list, max_length=2)
    question_repair_requests: list[str] = Field(default_factory=list, max_length=2)
    question_repair_decisions: list[RstQuestionRepair] = Field(default_factory=list, max_length=2)

    @model_validator(mode='after')
    def complete_question_history(self):
        if not (len(self.question_versions) == len(self.question_repair_requests) == len(self.question_repair_decisions)):
            raise ValueError('Question repair checkpoint must preserve snapshot, decision and request together')
        return self


class RstRound(StrictModel):
    generation: int
    parent_id: str | None
    attempts: list[RstAttempt] = Field(default_factory=list)
    candidate_id: str | None = None
    disposition: str = 'pending'
    prefilter_passed: bool = False


class RstProgress(StrictModel):
    input_sha256: str
    lines: dict[str, list[RstRound]] = Field(default_factory=dict)


class RstCurriculum(Pipeline):
    def __init__(self, *args, plan, pricing, **kwargs):
        super().__init__(*args, **kwargs)
        if self.settings.synthesis_protocol != 'rst_curriculum':
            raise ValueError('Use the explicit RST research config; production policy is unchanged')
        self.plan, self.pricing = plan, pricing
        self.objects = getattr(self.store, 'objects', None) or LocalObjects(self.run_dir / 'rst-objects')
        self.protocol = rst_protocol(self.settings, self.prompts)
        self.rst_pointer = self.store.runs().get(self.run_id, {}).get('rst_checkpoint')
        self.progress = RstProgress(input_sha256=digest(plan.model_dump()))
        if self.rst_pointer:
            self.progress = RstProgress.model_validate(self.objects.get(*self.rst_pointer))
            if self.progress.input_sha256 != digest(plan.model_dump()):
                raise ValueError('Frozen RST plan changed')
        self.finalize_lock = asyncio.Lock()

    def metadata(self, status):
        super().metadata(status)
        if getattr(self, 'rst_pointer', None):
            body = self.store.runs()[self.run_id]
            self.store.run(self.run_id, {**body, 'rst_checkpoint': self.rst_pointer})

    def checkpoint(self):
        self.rst_pointer = list(self.objects.put(self.progress.model_dump()))
        self.metadata('running')
        self.report()

    def report(self):
        own = [c for c in self.store.candidates() if c.run_id == self.run_id]
        lines = []
        for seed in self.plan.seeds:
            rows = []
            for record in self.progress.lines.get(seed.name, []):
                c = next((c for c in own if c.id == record.candidate_id), None)
                rows.append({"generation": record.generation, "parent_id": record.parent_id,
                    "candidate_id": record.candidate_id, "disposition": record.disposition,
                    "proposal_attempts": len(record.attempts), "proposal_errors": [a.errors for a in record.attempts],
                    "question_repairs": sum(len(a.question_repair_requests) for a in record.attempts),
                    "burden_rejections": sum(any(e.startswith('non_regression_') for e in a.errors)
                                             for a in record.attempts),
                    "non_regression_verified": bool(c and c.generation > 0 and valid_non_regression(c)),
                    "samples": len(sample_outcomes(c)) if c else 0, "answered": len(c.trials) if c else 0,
                    "deadline_incorrect": len(c.deadline_outcomes) if c else 0,
                    "correct": sum(t.judgment.correct == 'yes' for t in c.trials) if c else 0,
                    "proof_entries": len(c.solution.derivation) if c else None,
                    "scientific_prefilter_passed": record.prefilter_passed,
                    "status": c.status if c else None})
            lines.append({"lineage": seed.name, "category": seed.candidate.item.category, "rounds": rows})
        cost = measure_cost(self.store, self.run_id, self.run_dir / 'requests', self.settings, self.pricing)
        result = {"run_id": self.run_id, "pilot_only": True, "delivery_items": 0, "planned_lineages": len(self.plan.seeds),
            "planned_growth_rounds": self.plan.growth_rounds, "maximum_planned_versions": len(self.plan.seeds)*(1+self.plan.growth_rounds),
            "formed_versions": len(own), "retained_lineages": sum(any(r['disposition'] == 'in_band' for r in l['rounds'])
                for l in lines), "lines": lines, "cost": cost,
            "note": '科学通过可作为研究种子；4/4不是交付。各代样本小且相关，不证明难度逐轮单调增长。'}
        atomic_json(self.run_dir / 'rst-report.json', result)
        return result

    async def audit_item(self, c):
        source = self.store.get_source(c.source_ids[0]) if hasattr(self.store, 'get_source') else next(
            s for s in self.store.sources() if s.id == c.source_ids[0])
        c.audit, _, _ = await self.gateway.call('auditor', self.prompts['audit'] + '\n' + json.dumps({
            "item": c.item.model_dump(), "solution": c.solution.model_dump(), "source_fidelity": c.source_fidelity,
            "known_result": c.known_result.model_dump(), "locked_required_outputs": c.item.required_outputs,
            "source": source.model_dump()}, ensure_ascii=False), c.id + ':rst-question-audit:' + digest(c.item.model_dump()), Audit)

    async def seed(self, spec, row):
        source = next(s for s in self.store.sources() if s.id == spec.candidate.source_ids[0])
        c = spec.candidate.model_copy(deep=True)
        c.id = c.root_id = digest({"run": self.run_id, "seed": spec.sha256, "name": spec.name})[:24]
        c.parent_id, c.generation, c.run_id, c.operator = None, 0, self.run_id, 'rst_seed_recheck'
        c.source_fidelity = self.settings.source_fidelity
        c.trials, c.deadline_outcomes, c.reasons = [], [], []
        c.consistency = c.bottleneck_assessment = c.verifier_assessment = c.preflight_repair = None
        c.verifier_history, c.difficulty_rounds, c.difficulty_origin = [], [], None
        c.authoring_complete, c.status = True, 'quality_seed'
        c.origin_seed_sha256, c.origin_candidate_id = spec.sha256, spec.candidate.id
        c.rst_origin = None
        data = source.model_dump()
        target = {"category": c.item.category, "subtopic": c.item.subtopic}
        review, rid, model = await self.review_blueprint(c.solution, data, target,
            c.id + ':rst-seed-science', c.known_result)
        if not review.passed(source_fidelity='advisory'):
            row.disposition = 'seed_science_rejected'
            row.attempts = [RstAttempt(candidate_id=c.id, blueprint=review, errors=['seed_science_rejected'])]
            self.checkpoint()
            return None
        c.blueprint_audit, c.blueprint_request, c.blueprint_actual_model = review, rid, model
        c.blueprint_fingerprint = self.blueprint_stamp(c.solution, data, target, c.known_result)
        c.rst_origin = RstOrigin(protocol_sha256=self.protocol, source_sha256=source.sha256,
            contract_sha256=contract(c), imported_seed_sha256=spec.sha256)
        await self.audit_item(c)
        self.store.save(c)
        row.candidate_id = c.id
        self.checkpoint()
        return c

    async def grow(self, spec, parent, row):
        source = next(s for s in self.store.sources() if s.id == parent.source_ids[0])
        category = parent.item.category
        allowed = sorted(q['subtopic'] for q in self.quotas['quotas'] if q['category'] == category)
        operator = OPERATORS[(row.generation - 1) % len(OPERATORS)]
        for index in range(self.plan.proposal_attempts):
            cid = digest({"run": self.run_id, "parent": parent.id, "generation": row.generation, "attempt": index})[:24]
            if index < len(row.attempts) and row.attempts[index].errors:
                continue
            if index >= len(row.attempts):
                row.attempts.append(RstAttempt(candidate_id=cid))
                self.checkpoint()
            attempt = row.attempts[index]
            packet = {"source": source.model_dump(), "parent": parent_snapshot(parent).model_dump(),
                "parent_solver_feedback": [{"response": t.response, "correct": t.judgment.correct} for t in parent.trials],
                "category": category, "allowed_subtopics": allowed, "operator": operator,
                "difficulty_profile": self.difficulty_profile, "generation": row.generation,
                "previous_attempt": row.attempts[index-1].model_dump() if index else None}
            try:
                if attempt.proposal is None:
                    proposal, rid, _ = await self.gateway.call('generator', self.prompts['rst_grow'] + '\n' +
                        json.dumps(packet, ensure_ascii=False), cid + ':rst-grow', RstProposal)
                    attempt.proposal, attempt.proposal_request = proposal, rid
                    self.checkpoint()
                proposal = attempt.proposal
                if proposal.outcome == 'unavailable':
                    attempt.errors = ['growth_unavailable']
                    self.checkpoint()
                    continue
                if proposal.subtopic not in allowed:
                    attempt.errors = ['invalid_child_subtopic']
                    self.checkpoint()
                    continue
                solution = proposal.proof.bind(proposal.result)
                from .difficulty import bottleneck_mask_errors, difficulty_design_errors, known_result_errors
                errors = known_result_errors(proposal.result, {source.id: source}, source_fidelity='advisory')
                errors += bottleneck_mask_errors(solution)
                errors += difficulty_design_errors(solution, category, self.difficulty_profile, True)
                target = {"category": category, "subtopic": proposal.subtopic}
                if errors:
                    attempt.errors = errors
                    self.checkpoint()
                    continue
                review, bid, bmodel = await self.review_blueprint(solution, source.model_dump(), target,
                    cid + ':rst-child-science', proposal.result,
                    construction_reference={'locked_required_outputs': proposal.required_outputs})
                attempt.blueprint = review
                if not review.passed(source_fidelity='advisory'):
                    attempt.errors = ['child_science_rejected']
                    self.checkpoint()
                    continue
                growth, gid, gmodel = await self.consistency_call(self.prompts['rst_growth_audit'] + '\n' +
                    json.dumps({"parent": parent_snapshot(parent).model_dump(), "proposal": proposal.model_dump(),
                        "child_solution": solution.model_dump(), "operator": operator}, ensure_ascii=False),
                    cid + ':rst-growth-audit', RstGrowthAudit)
                attempt.growth = growth
                if (not growth.passed() or max(growth.parent_proof_entries, default=0) > len(parent.solution.derivation)
                        or max(growth.child_proof_entries, default=0) > len(solution.derivation)):
                    attempt.errors = ['no_verified_structural_growth']
                    self.checkpoint()
                    continue
                if attempt.non_regression is None:
                    check, nid, nmodel = await review_non_regression(self.consistency_call,
                        self.prompts['rst_non_regression'], parent_snapshot(parent),
                        salt=cid + ':rst-non-regression:proposal', phase='proposal', solution=solution,
                        known_result=proposal.result, required_outputs=proposal.required_outputs,
                        category=category, subtopic=proposal.subtopic)
                    attempt.non_regression, attempt.non_regression_request = check, nid
                    attempt.non_regression_actual_model = nmodel
                    self.checkpoint()
                errors = regression_errors(attempt.non_regression, 'proposal')
                if errors:
                    attempt.errors = errors
                    self.checkpoint()
                    continue
                draft, aid, _ = await self.gateway.call('generator', self.prompts['rst_align'] + '\n' +
                    json.dumps({"solution": solution.model_dump(), "known_result": proposal.result.model_dump(),
                        "target": target, "locked_required_outputs": proposal.required_outputs},
                        ensure_ascii=False), cid + ':rst-align', RstQuestionDraft)
                item = Question(**draft.model_dump(), answer=solution.answer, answer_type='exactMatch',
                    category=category, subtopic=proposal.subtopic, assumptions=solution.assumptions,
                    evidence=solution.evidence, required_outputs=proposal.required_outputs)
                c = Candidate(id=cid, parent_id=parent.id, root_id=parent.root_id, generation=row.generation,
                    operator=operator, source_ids=[source.id], source_fidelity='advisory', solution=solution,
                    item=item, known_result=proposal.result, blueprint_audit=review, blueprint_request=bid,
                    blueprint_actual_model=bmodel, blueprint_fingerprint=self.blueprint_stamp(
                        solution, source.model_dump(), target, proposal.result), status='quality_seed',
                    audit=Audit(**dict.fromkeys(Audit.model_fields.keys()-{'reason'}, False), reason='Pending independent audit.'),
                    run_id=self.run_id)
                snap = parent_snapshot(parent)
                c.rst_origin = RstOrigin(protocol_sha256=self.protocol, source_sha256=source.sha256,
                    contract_sha256=contract(c), parent_contract_sha256=digest(snap.model_dump()), parent_snapshot=snap,
                    proposal_request=attempt.proposal_request, alignment_request=aid,
                    growth_audit=growth, growth_audit_request=gid, growth_actual_model=gmodel)
                check, nid, nmodel = await review_non_regression(self.consistency_call,
                    self.prompts['rst_non_regression'], snap,
                    salt=cid + ':rst-non-regression:question:' + question_contract(c), phase='question', solution=solution,
                    known_result=proposal.result, required_outputs=item.required_outputs,
                    category=category, subtopic=item.subtopic, item=item)
                c.rst_origin.non_regression = RstNonRegressionEvidence(parent_sha256=digest(snap.model_dump()),
                    proposal_sha256=proposal_contract(c), question_sha256=question_contract(c),
                    proposal=attempt.non_regression, proposal_request=attempt.non_regression_request,
                    proposal_actual_model=attempt.non_regression_actual_model,
                    question=check, question_request=nid, question_actual_model=nmodel)
                errors = regression_errors(check, 'question')
                if errors:
                    attempt.errors = errors
                    c.status, c.reasons = 'rejected', errors
                    self.store.save(c)
                    self.checkpoint()
                    continue
                await self.audit_item(c)
                self.store.save(c)
                row.candidate_id = c.id
                self.checkpoint()
                return c
            except ModelResponseError as error:
                attempt.errors = ['unverified_model_response:' + error.reason]
                self.checkpoint()
        row.disposition = 'growth_rejected_or_unverified'
        self.checkpoint()
        return None

    async def prepare_for_measure(self, c, row):
        while True:
            attempt = row.attempts[-1] if row.attempts else None
            if attempt and attempt.question_repair_decisions:
                decision = attempt.question_repair_decisions[-1]
                if decision.outcome != 'question_contract_only':
                    return False
                old = attempt.question_versions[-1]
                rid = attempt.question_repair_requests[-1]
                expected = Question.model_validate({**old.item.model_dump(), **decision.revised.model_dump()})
                if c.solution != old.solution or c.known_result != old.known_result:
                    raise CacheIntegrityError('Locked RST proof/gold changed during question repair')
                if c.item == old.item and c.rst_origin.alignment_request == old.rst_origin.alignment_request:
                    c.item = expected
                    c.consistency = c.bottleneck_assessment = c.preflight_repair = None
                    c.status, c.reasons = 'quality_seed', []
                    c.authoring_repairs_completed = len(attempt.question_repair_requests)
                    c.rst_origin.alignment_request = rid
                    c.rst_origin.contract_sha256 = contract(c)
                    # 修题面会使实际负担审查失效；在任何后续科学/采样前重查，不能借修复泄露桥梁。
                    check, nid, nmodel = await review_non_regression(self.consistency_call,
                        self.prompts['rst_non_regression'], c.rst_origin.parent_snapshot,
                        salt=c.id + ':rst-non-regression:question:' + question_contract(c), phase='question',
                        solution=c.solution, known_result=c.known_result, required_outputs=c.item.required_outputs,
                        category=c.item.category, subtopic=c.item.subtopic, item=c.item)
                    evidence = c.rst_origin.non_regression
                    evidence.question_sha256, evidence.question = question_contract(c), check
                    evidence.question_request, evidence.question_actual_model = nid, nmodel
                    errors = regression_errors(check, 'question')
                    if errors:
                        c.status, c.reasons = 'rejected', errors
                        self.store.save(c)
                        self.checkpoint()
                        return False
                    await self.audit_item(c)
                    self.store.save(c)
                    self.log('rst_question_contract_repaired', candidate=c.id,
                        revision=c.authoring_repairs_completed, golden_changed=False,
                        proof_changed=False, verifier_entered=False)
                    self.checkpoint()
                elif c.item != expected or c.rst_origin.alignment_request != rid:
                    raise CacheIntegrityError('Unrecorded RST question mutation during repair recovery')
            if await self.preflight(c):
                return True
            # 子题无样本且尚未进入verifier才可定向修题面；证书失败不能让作者改题规避。
            advice = c.preflight_repair
            repairable = ((advice and advice.owner == 'author') or
                          (c.reasons and all(r.startswith('audit_failed:') for r in c.reasons)))
            if (not attempt or not repairable or c.trials or c.deadline_outcomes or c.verifier_assessment
                    or c.verifier_history or len(attempt.question_repair_requests) >= min(2, self.settings.max_repairs)):
                return False
            index = len(attempt.question_repair_requests)
            decision, rid, _ = await self.gateway.call('generator', self.prompts['rst_question_repair'] + '\n' +
                json.dumps({'item': c.item.model_dump(), 'known_result': c.known_result.model_dump(),
                    'solution': c.solution.model_dump(), 'locked_required_outputs': c.item.required_outputs,
                    'failures': c.reasons, 'classification': advice.model_dump() if advice else None},
                    ensure_ascii=False), f'{c.id}:rst-question-repair:{index}', RstQuestionRepair)
            attempt.question_versions.append(c.model_copy(deep=True))
            attempt.question_repair_requests.append(rid)
            attempt.question_repair_decisions.append(decision)
            self.checkpoint()

    async def measure(self, c, row):
        if not await self.prepare_for_measure(c, row):
            row.disposition = 'quality_rejected_or_unverified'
            self.checkpoint()
            return False
        row.prefilter_passed = True
        self.checkpoint()
        first = await self.sample(c, 4)
        if first == 4:
            c.status, c.reasons = 'rejected', ['too_easy:first_four_all_correct']
            self.store.save(c)
            row.disposition = 'easy_research_seed'
            self.log('rst_reseed_easy', candidate=c.id, generation=c.generation, correct=4, samples=4,
                delivery_eligible=False, parent_mutated=False)
            self.checkpoint()
            return True
        n = await self.sample(c)
        if n <= 8 and not await QuestionExperiment.final_audit(self, c):
            row.disposition = 'post_audit_rejected'
            self.checkpoint()
            return False
        if n == 0:
            c.status, c.reasons = 'rejected', ['rst_stop:zero_correct_after_scientific_recheck']
            self.store.save(c)
            row.disposition = 'too_hard_stop'
        elif n <= 8:
            async with self.finalize_lock:
                await self.evaluate(c)
            row.disposition = 'in_band' if c.status in ('screened', 'accepted') else 'postfilter_rejected'
        else:
            c.status, c.reasons = 'rejected', ['too_easy:complete_16_above_max']
            self.store.save(c)
            row.disposition = 'easy_research_seed'
        self.checkpoint()
        return row.disposition == 'easy_research_seed'

    async def run_curriculum(self):
        self.start({"command": 'rst-curriculum', "plan_sha256": digest(self.plan.model_dump()),
            "pricing_sha256": digest(self.pricing), "protocol_sha256": self.protocol})
        sem = asyncio.Semaphore(self.plan.root_workers)

        async def one(spec):
            async with sem:
                rows = self.progress.lines.setdefault(spec.name, [])
                parent = None
                for generation in range(self.plan.growth_rounds + 1):
                    if generation >= len(rows):
                        rows.append(RstRound(generation=generation, parent_id=parent.id if parent else None))
                        self.checkpoint()
                    row = rows[generation]
                    c = next((c for c in self.store.candidates() if c.id == row.candidate_id), None)
                    if row.disposition != 'pending':
                        if row.disposition != 'easy_research_seed':
                            return
                        parent = c
                        continue
                    try:
                        c = c or (await self.seed(spec, row) if generation == 0 else await self.grow(spec, parent, row))
                        if c is None or not await self.measure(c, row):
                            return
                        parent = c
                    except (ModelResponseError, SampleIncomplete, QualityRejected) as error:
                        row.disposition = 'unverified:' + type(error).__name__
                        self.log('rst_round_unverified', lineage=spec.name, generation=generation,
                            reason=str(error)[:180], solver_failure_is_incorrect=False)
                        self.checkpoint()
                        return
        try:
            await gather_cancel_on_error(*(one(seed) for seed in self.plan.seeds))
        except BaseException as error:
            self.metadata('budget_exhausted' if isinstance(error, BudgetExceeded) else 'interrupted')
            self.report()
            raise
        incomplete = any(any('unverified' in r.disposition for r in rows) for rows in self.progress.lines.values())
        self.metadata('completed_with_errors' if incomplete else 'completed')
        return self.report()
