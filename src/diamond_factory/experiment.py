"""Bounded, breadth-first paper-to-question experiments with a 4 -> 16 sampling gate."""
import asyncio
import json
import math
from itertools import pairwise

from pydantic import Field

from .consistency import QualityRejected, has_valid_consistency
from .costs import measure_cost
from .gates import correct_count, sample_outcomes
from .llm import BudgetExceeded, ModelResponseError
from .models import Audit, StrictModel, digest
from .pipeline import AuthoringRejected, Pipeline, SampleIncomplete
from .runtime import atomic_json, gather_cancel_on_error


class RootPlan(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    source_id: str
    category: str
    subtopic: str
    focus: str = Field(min_length=20)


class ExperimentPlan(StrictModel):
    roots: list[RootPlan] = Field(min_length=1, max_length=100)
    max_revisions: int = Field(default=2, ge=0, le=3)
    root_workers: int = Field(default=4, ge=1, le=16)
    visible_cost_limit_usd: float | None = Field(default=90.0, gt=0)
    resume_request_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


def branch(candidate):
    if candidate.status in ("screened","accepted"):
        return "retained"
    if candidate.status == "rejected":
        if any(r in ("too_easy:first_four_all_correct", "too_easy:complete_16_above_max")
               for r in candidate.reasons):
            return "too_easy_rejected"
        return "quality_rejected"
    if len(sample_outcomes(candidate)) == 4 and correct_count(sample_outcomes(candidate),4) == 4:
        return "preliminary_harden"
    if len(sample_outcomes(candidate)) != 16:
        return "incomplete"
    n = correct_count(sample_outcomes(candidate))
    return "ease" if n == 0 else "harden" if n > 8 else "in_band_unfinalized"


def golden_context(c):
    """初始锁定结论及完整参考蓝图均为待验证主张，不向盲解或GLM泄露。"""
    return {'locked_source_result':c.known_result.model_dump() if c.known_result else None,
            'reference_solution':c.solution.model_dump(),
            'blueprint_review':c.blueprint_audit.model_dump() if c.blueprint_audit else None,
            'blueprint_fingerprint':c.blueprint_fingerprint,
            'authoring_repair_index':c.authoring_repairs_completed}


def wilson(k,n):
    if not n:
        return None
    z = 1.96
    p = k/n
    denom = 1+z*z/n
    mid = (p+z*z/(2*n))/denom
    half = z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/denom
    return [max(0,mid-half),min(1,mid+half)]


class QuestionExperiment(Pipeline):
    def __init__(self,*args,plan,pricing,**kwargs):
        super().__init__(*args,**kwargs)
        self.plan,self.pricing = plan,pricing
        self.plan_state = {}
        self.finalize_lock = asyncio.Lock()

    def snapshot(self):
        own = [c for c in self.store.candidates() if c.run_id == self.run_id]
        trees = []
        retained = 0
        initial = 0
        transitions = []
        sources = {s.id:s for s in self.store.sources()}
        for root in self.plan.roots:
            root_id = self.root_identity(root,sources)
            lineage = sorted([c for c in own if c.root_id == root_id],key=lambda c:c.generation)
            passed = [c for c in lineage if c.status in ("screened","accepted")]
            retained += bool(passed)
            initial += bool(passed and passed[0].generation == 0)
            versions = [{"id":c.id,"generation":c.generation,"parent_id":c.parent_id,
                         "decision":branch(c),"samples":len(sample_outcomes(c)),
                         "answered_samples":len(c.trials), "deadline_incorrect_samples":len(c.deadline_outcomes),
                         "consistency_passed":has_valid_consistency(c, sources),
                         "difficulty_measurement_valid":(c.audit.passed()
                             and has_valid_consistency(c, sources) and len(sample_outcomes(c))==16),
                         "observed_correct":sum(t.judgment.correct=="yes" for t in c.trials),
                         "status":c.status,"reasons":c.reasons} for c in lineage]
            for a,b in pairwise(versions):
                transitions.append({"root":root.name,"from":a["decision"],"to":b["decision"],
                                    "from_id":a["id"],"to_id":b["id"]})
            trees.append({"root":root.name,"category":root.category,"source_id":root.source_id,
                          "state":self.plan_state.get(root.name,{}),"versions":versions})
        cost = measure_cost(self.store,self.run_id,self.run_dir/"requests",self.settings,self.pricing)
        n = len(self.plan.roots)
        result = {"run_id":self.run_id,"pilot_only":True,"delivery_items":0,"planned_independent_roots":n,
                  "generated_versions":len(own),"roots_with_generated_question":sum(bool(t["versions"]) for t in trees),
                  "first_generation_retained":initial,"first_generation_yield":initial/n,
                  "roots_with_retained_question":retained,"bounded_iteration_yield":retained/n,
                  "yield_wilson_95_descriptive":wilson(retained,n),"cost":cost,
                  "known_api_cost_per_planned_root_usd":cost["known_cost_usd"]/n,
                  "known_api_cost_per_retained_root_usd":cost["known_cost_usd"]/retained if retained else None,
                  "preliminary_4_of_4_versions":sum(branch(c)=="preliminary_harden" for c in own),
                  "transitions":transitions,"roots":trees,
                  "interpretation":"Small convenience sample; staged selection, not an unbiased model accuracy "
                                   "estimate or causal proof of RST. Machine QC is not expert scientific validation."}
        atomic_json(self.run_dir/"experiment-report.json",result)
        atomic_json(self.run_dir/"candidates.json",[c.model_dump() for c in own])
        return result

    def brief(self,root,parent):
        return {"root":root.name,"focus":root.focus,
                "parent_measurement":branch(parent) if parent else "none"}

    def root_identity(self,root,sources):
        return self.generation_identity(sources[root.source_id],None,
             {"category":root.category,"subtopic":root.subtopic},0,0,self.brief(root,None))

    def generation_identity(self, source, parent, target, generation, attempt, brief=None):
        # 续跑只保留首代请求命名空间；内容仍须通过网关逐字及完整性校验，降难使用新run。
        if generation == 0 and parent is None and self.plan.resume_request_run_id:
            return digest({"run": self.plan.resume_request_run_id, "source": source.id, "parent": None,
                           "generation": generation, "attempt": attempt, "target": target, "brief": brief})[:24]
        return super().generation_identity(source, parent, target, generation, attempt, brief)

    async def final_audit(self,c):
        sources = {s.id:s for s in self.store.sources()}
        assessed = [{"sample_id":t.sample_id,"response":t.response[:12000],
                          "response_excerpted":len(t.response)>12000,
                          "untrusted_judge_claim":t.judgment.model_dump()}
                         for t in c.trials]
        prompt = ("Independently re-derive the answer from scratch. Inspect well-posedness, unique solution, "
                  "all algebraic branches, units, physical feasibility, hidden assumptions and global versus "
                  "local extrema. Treat the supplied derivation as an untrusted proposal. A finite numerical "
                  "example cannot prove a global assertion. Do not infer correctness from solver agreement. "
                  "Reject if any essential step is unjustified. For an intentional easier revision, a "
                  "substantive removal of one inferential burden is valid. Inspect every supplied response, including those marked correct, and "
                  "judge claims as untrusted evidence: could a response be correct "
                  "under the actual question, e.g. a reciprocal ratio or equivalent branch encoding whose "
                  "convention was never specified? Do not invent a convention from the gold. If such a "
                  "grading ambiguity exists, reject this assessment; do not silently edit old judgments. "
                  "For a bound request, inspect whether the question specifies a canonical/tight bound "
                  "or also permits a valid weaker bound. Do not label the latter a physical contradiction. "
                  "Adversarially check the initial locked golden and full reference proof against the source; "
                  "neither the golden nor its previous blueprint review is axiomatic. Identify whether an "
                  "issue belongs to the golden/proof, question, solver answer, judge extraction or scoring "
                  "contract. Do not conflate a judge error with a physical error. "
                  "Check all requested outputs appear in the gold. Test claimed physical regimes against "
                  "the entire proposed domain: lossless scattering must conserve energy and exact matrix "
                  "identities must hold, beyond a least-squares fit. Try an explicit admissible counterexample. "
                  "A physical contradiction cannot be used as difficulty. Return the complete Audit schema.\n"+
                  json.dumps({"item":c.item.model_dump(),"initial_golden":golden_context(c),
                              "graded_responses":assessed,
                              "solver_deadline_outcomes":[d.model_dump() for d in c.deadline_outcomes],
                              "timeout_policy":"A solver deadline counts as incorrect by user protocol, but is no mathematical counterexample.",
                              "source_texts":[sources[s].text[:self.settings.max_source_chars] for s in c.source_ids]},
                             ensure_ascii=False))
        audit,rid,model = await self.gateway.call("auditor",prompt,c.id+":post-evaluation-audit-v3",Audit)
        self.log("post_evaluation_audit",candidate=c.id,request_id=rid,actual_model=model,passed=audit.passed())
        c.audit = audit
        if not audit.passed():
            c.status,c.reasons = "rejected",["post_audit_failed:"+audit.reason]
        self.store.save(c)
        return audit.passed()

    async def run_experiment(self):
        if self.settings.early_screen_samples != 4 or self.settings.samples != 16:
            raise ValueError("Experiment requires the explicit 4 -> 16 protocol")
        sources = {s.id:s for s in self.store.sources()}
        allowed = {(q["category"],q["subtopic"]) for q in self.quotas["quotas"]}
        if len({r.name for r in self.plan.roots}) != len(self.plan.roots):
            raise ValueError("Root names must be unique")
        for r in self.plan.roots:
            if r.source_id not in sources or (r.category,r.subtopic) not in allowed:
                raise ValueError("Missing full text or invalid v3 target for "+r.name)
        self.start({"command":"paper-question-experiment","plan_sha256":digest(self.plan.model_dump()),
                    "pricing_sha256":digest(self.pricing)})
        permits = asyncio.Semaphore(self.plan.root_workers)

        async def prefix(root,generation):
            async with permits:
                existing = [c for c in self.store.candidates() if c.run_id==self.run_id and
                            c.root_id==self.root_identity(root,sources)]
                parent = next((c for c in existing if c.generation==generation-1),None) if generation else None
                allowed = ("ease",) if self.settings.difficulty_policy == "ease_only" else (
                    "preliminary_harden", "harden", "ease")
                if generation and (parent is None or branch(parent) not in allowed):
                    return None
                if any(c.status in ("screened","accepted") for c in existing):
                    return None
                try:
                    target = {"category":root.category,"subtopic":root.subtopic}
                    c = await self.generate(sources[root.source_id],parent,target,generation,0,
                                            brief=self.brief(root,parent))
                    if c.status == "rejected":
                        return c
                    if len(sample_outcomes(c)) < 16:
                        n = await self.sample(c,4)
                        if n == 4:
                            await self.evaluate(c)
                    return c
                except QualityRejected:
                    return next((c for c in self.store.candidates() if c.run_id == self.run_id
                                 and c.root_id == self.root_identity(root, sources)
                                 and c.generation == generation), None)
                except AuthoringRejected as error:
                    self.plan_state[root.name] = {"phase":"authoring_rejected","reason":str(error)[:400]}
                    self.log("experiment_authoring_rejected",root=root.name,generation=generation,reason=str(error)[:400])
                    return None
                except (ModelResponseError,SampleIncomplete) as error:
                    self.plan_state[root.name] = {"phase":"prefix_failed","reason":str(error)[:180]}
                    self.log("experiment_root_incomplete",root=root.name,generation=generation,
                             reason=str(error)[:180])
                    return None
                finally:
                    self.snapshot()

        async def finish(c):
            if c is None or c.status=="rejected" or branch(c)=="preliminary_harden":
                return
            async with permits:
                try:
                    n = await self.sample(c)
                    if 0 <= n <= 8 and not await self.final_audit(c):
                        return
                    # Dedup finalization is serialized so two parallel candidates cannot miss each other.
                    async with self.finalize_lock:
                        await self.evaluate(c)
                except QualityRejected:
                    return
                except (ModelResponseError,SampleIncomplete) as error:
                    self.log("experiment_candidate_incomplete",candidate=c.id,reason=str(error)[:180])
                finally:
                    self.snapshot()

        try:
            for generation in range(self.plan.max_revisions+1):
                self.log("experiment_round",generation=generation,roots=len(self.plan.roots))
                rows = await gather_cancel_on_error(*(prefix(r,generation) for r in self.plan.roots))
                await gather_cancel_on_error(*(finish(c) for c in rows))
        except BaseException as error:
            self.metadata("budget_exhausted" if isinstance(error,BudgetExceeded) else "interrupted")
            self.snapshot()
            raise
        final = self.snapshot()
        missing = (final["roots_with_generated_question"]<len(self.plan.roots) or any(
            v["decision"]=="incomplete" for r in final["roots"] for v in r["versions"]) or
            any(state.get("phase")=="prefix_failed" for state in self.plan_state.values()))
        self.metadata("completed_with_errors" if missing else "completed")
        return self.snapshot()
