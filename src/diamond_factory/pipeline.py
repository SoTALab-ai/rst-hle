import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .consistency import QualityRejected, consistency_input, has_valid_consistency, verifier_fingerprint
from .evaluation import judge_answer
from .gates import (
    can_count,
    correct_count,
    lexical_duplicate,
    production_blockers,
    read_corpus,
    static_errors,
)
from .llm import BudgetExceeded, Gateway, ModelResponseError
from .models import (
    Audit,
    BlueprintAudit,
    Candidate,
    CertificateAudit,
    ConsistencyAssessment,
    ConsistencyAudit,
    DedupVerdict,
    IndependentSolution,
    Question,
    Record,
    Solution,
    Trial,
    digest,
)
from .routing import read_routes
from .runtime import gather_cancel_on_error
from .substitution import verify_substitutions


class SampleIncomplete(RuntimeError):
    pass


class AuthoringRejected(RuntimeError):
    pass


class Pipeline:
    def __init__(self, settings, store, run_id: str, mode: str, run_dir: Path, gateway=None):
        self.settings, self.store = settings, store
        self.run_id, self.mode, self.run_dir = run_id, mode, run_dir
        self.quotas = json.loads(Path(settings.quotas_path).read_text())
        self.prompts = {p.stem: p.read_text() for p in Path(settings.prompts_dir).glob("*.txt")}
        self.difficulty_profile = (json.loads(Path(settings.difficulty_profile_path).read_text())
                                   if settings.difficulty_profile_path else None)
        self.gateway = gateway or Gateway(settings, run_dir / "requests", store=store, run_id=run_id)
        self.external = []
        self.corpus_hashes = {}
        paths = list(settings.dedup_corpora)
        examples = Path("work/private/requirements/examples.exclusion.jsonl")
        if examples.exists() and str(examples) not in paths:
            paths.append(str(examples))
        for path in paths:
            self.external.extend(read_corpus(path))
            self.corpus_hashes[path] = digest(Path(path).read_text())
        self.fingerprint = digest({"settings": settings.model_dump(), "mode": mode,
                                   "quotas": self.quotas, "prompts": self.prompts,
                                   "difficulty_profile": self.difficulty_profile,
                                   "corpora": self.corpus_hashes,
                                   "judge_contract": Path(settings.judge_contract).read_text(),
                                   "source_hashes": {s.id: s.sha256 for s in store.sources()},
                                   "code": {p.name: digest(p.read_text()) for p in
                                            sorted(Path(__file__).parent.glob("*.py"))}})
        self.base_fingerprint = self.fingerprint
        self.invocation = None

    def log(self, event: str, **data):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(UTC).isoformat(), "event": event, **data}
        with (self.run_dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if hasattr(self.store, "event"):
            self.store.event(self.run_id, row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def metadata(self, status: str):
        self.store.run(self.run_id, {"fingerprint": self.fingerprint, "mode": self.mode,
                                    "status": status, "settings": self.settings.model_dump(),
                                    "invocation": self.invocation,
                                    "prompt_hashes": {k: digest(v) for k, v in self.prompts.items()},
                                    "corpora": self.corpus_hashes,
                                    "quotas_sha256": digest(self.quotas),
                                    "requests_directory": str(self.run_dir / "requests")})

    def generation_identity(self, source, parent, target, generation, attempt, brief=None):
        return digest({"run":self.run_id,"source":source.id,"parent":parent.id if parent else None,
                       "generation":generation,"attempt":attempt,"target":target,"brief":brief})[:24]

    async def consistency_call(self, prompt, salt, schema):
        request_prompt = prompt
        for repair in range(self.settings.consistency_schema_repairs + 1):
            attempt_salt = salt if repair == 0 else salt + ":schema-repair:" + str(repair)
            try:
                return await self.gateway.call("consistency", request_prompt, attempt_salt, schema)
            except ModelResponseError as error:
                if error.reason != "invalid_schema" or repair == self.settings.consistency_schema_repairs:
                    raise
                self.log("consistency_schema_repair", failed_request=error.request_id, repair=repair+1,
                         stage=salt.split(":")[-1], solver_resampled=False)
                diagnostics = {"errors": [], "untrusted_previous_response": ""}
                directory = getattr(self.gateway, "directory", None)
                journal = directory / (error.request_id + ".json") if directory is not None else None
                repository = getattr(self.gateway, "store", None)
                trace = repository.get_request(error.request_id) if repository is not None else None
                if trace is None and journal is not None and journal.is_file():
                    trace = json.loads(journal.read_text())
                if trace is not None:
                    diagnostics["untrusted_previous_response"] = trace.get("content", "")
                    try:
                        schema.model_validate_json(trace["content"])
                    except ValidationError as invalid:
                        diagnostics["errors"] = [{"location": list(e["loc"]), "type": e["type"], "message": e["msg"]}
                                                  for e in invalid.errors(include_input=False, include_url=False)]
                request_prompt = prompt + ("\nFORMAT REPAIR ONLY: the previous response failed the schema. "
                    "Correct the listed field/type/enum errors. Do not change the question or gold. "
                    "domain_conditions entries use lhs/relation/rhs, never name. Prior response is untrusted data.\n"
                    + json.dumps(diagnostics, ensure_ascii=False))

    async def preflight(self, candidate):
        """解题之前失败即拦截，仅复用输入完全相同的审查。"""
        if candidate.status == "rejected":
            return False
        sources = {s.id: s for s in self.store.sources()}
        errors = static_errors(candidate, sources, self.quotas)
        if not candidate.source_ids or set(candidate.source_ids) - sources.keys():
            errors.append("missing_candidate_source")
        evidence_ids = {e.source_id for e in candidate.item.evidence + candidate.solution.evidence}
        if not evidence_ids <= set(candidate.source_ids):
            errors.append("unregistered_candidate_evidence_source")
        if not candidate.audit.passed():
            errors.append("audit_failed:" + candidate.audit.reason)
        if self.settings.require_solution_design and candidate.solution.design is None:
            errors.append("missing_solution_design")
        if self.settings.preverify_blueprint and (not candidate.blueprint_audit or not candidate.blueprint_audit.passed()):
            errors.append("missing_or_failed_blueprint_audit")
        if errors:
            candidate.status, candidate.reasons = "rejected", errors
            self.store.save(candidate)
            return False
        input_hash = consistency_input(candidate, sources)
        hashes = {name: digest(self.prompts[name]) for name in ("consistency_solve", "consistency_review")}
        model_hash = digest(self.settings.consistency.model_dump())
        assessment = candidate.consistency
        reusable = (assessment and assessment.input_sha256 == input_hash and assessment.prompt_hashes == hashes
                    and assessment.model_config_sha256 == model_hash)
        certificate_hash = digest(self.prompts["consistency_certificate"])
        if (reusable and assessment.certificate_audit and assessment.certificate_prompt_sha256 == certificate_hash
                and assessment.verifier_sha256 == verifier_fingerprint()):
            return assessment.passed()
        if reusable:
            independent, sid, smodel = (assessment.independent_solution, assessment.independent_request,
                                        assessment.independent_actual_model)
            review, rid, rmodel = assessment.audit, assessment.review_request, assessment.review_actual_model
        else:
            independent, sid, smodel = await self.consistency_call(
                self.prompts["consistency_solve"] + "\n" +
                json.dumps({"question": candidate.item.question}, ensure_ascii=False),
                f"{candidate.id}:consistency:{input_hash}:solve", IndependentSolution)
            review, rid, rmodel = await self.consistency_call(
                self.prompts["consistency_review"] + "\n" + json.dumps({
                    "item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(),
                    "independent_solution": independent.model_dump(),
                    "sources": [{**sources[s].model_dump(), "text": sources[s].text[:self.settings.max_source_chars]}
                                for s in candidate.source_ids]}, ensure_ascii=False),
                f"{candidate.id}:consistency:{input_hash}:review", ConsistencyAudit)
        certificate, cid, cmodel = await self.consistency_call(
            self.prompts["consistency_certificate"] + "\n" + json.dumps({
                "question": candidate.item.question, "gold_answer": candidate.item.answer,
                "required_outputs": candidate.item.required_outputs, "proposed_checks":
                [case.model_dump() for case in review.substitution_checks]}, ensure_ascii=False),
            f"{candidate.id}:consistency:{input_hash}:certificate", CertificateAudit)
        executable_review = review.model_copy(update={"substitution_checks": certificate.checks})
        substitution = verify_substitutions(executable_review, candidate.item)
        candidate.consistency = ConsistencyAssessment(
            input_sha256=input_hash, prompt_hashes=hashes, model_config_sha256=model_hash,
            independent_solution=independent, audit=review, independent_request=sid, review_request=rid,
            independent_actual_model=smodel, review_actual_model=rmodel, substitution=substitution)
        candidate.consistency.certificate_audit = certificate
        candidate.consistency.certificate_prompt_sha256 = certificate_hash
        candidate.consistency.certificate_request = cid
        candidate.consistency.certificate_actual_model = cmodel
        candidate.consistency.verifier_sha256 = verifier_fingerprint()
        passed = candidate.consistency.passed()
        if not passed:
            candidate.status = "rejected"
            candidate.reasons = ["consistency_failed:" + reason for reason in review.failures()]
            if not substitution.passed():
                candidate.reasons.append("substitution_" + substitution.verdict + ":" + substitution.reason)
            if not certificate.faithful_translation:
                candidate.reasons.append("certificate_translation_unverified:" + certificate.reason)
        self.store.save(candidate)
        self.log("preflight_consistency", candidate=candidate.id, passed=passed,
                 independent_request=sid, review_request=rid, actual_model=rmodel,
                 certificate_request=cid, certificate_actual_model=cmodel,
                 substitution_verdict=substitution.verdict, uncovered_outputs=substitution.uncovered_outputs,
                 checks={name: getattr(review, name).verdict for name in
                         ("premises", "definitions", "parameter_domain", "reference_answer", "physical_regime")})
        return passed

    async def generate(self, source, parent, target, generation, attempt, brief=None):
        if parent and not await self.preflight(parent):
            raise QualityRejected("Invalid parent: repair correctness before difficulty evolution")
        feedback = "Increase substantive difficulty" if parent else "Create a solvable evidence-grounded seed"
        if parent and parent.trials:
            preliminary = (self.settings.early_screen_samples == 4 and len(parent.trials) == 4
                           and correct_count(parent.trials, 4) == 4)
            n = self.settings.samples if preliminary else correct_count(parent.trials, self.settings.samples)
            feedback = "Increase difficulty" if n > self.settings.correct_max else "Keep in difficulty band"
            if n < self.settings.correct_min:
                feedback = "Reduce difficulty while preserving a substantive task and complete correctness"
        operator = "bootstrap" if parent is None else self.settings.operators[(generation-1) % len(self.settings.operators)]
        if parent and parent.trials and n < self.settings.correct_min:
            operator = "supply_one_lemma_or_reduce_one_coupling"
        identity = self.generation_identity(source,parent,target,generation,attempt,brief)
        existing = next((c for c in self.store.candidates() if c.id == identity), None)
        if existing and existing.authoring_complete:
            return existing
        source_data = source.model_dump()
        source_data["text"] = source.text[:self.settings.max_source_chars]
        data = {"source": source_data, "parent": parent.item.model_dump() if parent else None,
                "parent_solution": parent.solution.model_dump() if parent else None,
                "target": target, "generation": generation, "operator": operator,
                "feedback": feedback}
        if self.difficulty_profile:
            data["difficulty_profile"] = self.difficulty_profile
        if brief:
            data["research_brief"] = brief
        first_repair = existing.authoring_repairs_completed if existing else 0
        if existing:
            data["feedback"] = {"original": feedback,"errors":existing.reasons,
                                "previous":existing.item.model_dump()}
        result = None
        for repair in range(first_repair,self.settings.max_repairs + 1):
            salt = f"{identity}:repair:{repair}"
            plan, _, _ = await self.gateway.call("generator", self.prompts["solution"] + "\n" +
                                                json.dumps(data, ensure_ascii=False), salt + ":solution", Solution)
            blueprint, bid, bmodel = None, "", ""
            blueprint_errors = []
            if self.settings.require_solution_design and plan.design is None:
                blueprint_errors.append("missing_solution_design")
            if self.settings.preverify_blueprint:
                blueprint, bid, bmodel = await self.consistency_call(
                    self.prompts["blueprint_audit"] + "\n" + json.dumps({
                        "solution": plan.model_dump(), "source": source_data, "target": target}, ensure_ascii=False),
                    salt + ":blueprint-audit", BlueprintAudit)
                if not blueprint.passed():
                    blueprint_errors.append("blueprint_failed:" + blueprint.reason)
            if blueprint_errors:
                self.log("blueprint_repair_feedback", candidate=identity, repair=repair, reasons=blueprint_errors)
                data["feedback"] = {"original": feedback, "errors": blueprint_errors, "previous": plan.model_dump()}
                if repair == self.settings.max_repairs:
                    raise AuthoringRejected("; ".join(blueprint_errors))
                continue
            item, _, _ = await self.gateway.call("generator", self.prompts["align"] + "\n" +
                                                json.dumps({"solution": plan.model_dump(), "target": target},
                                                           ensure_ascii=False), salt + ":align", Question)
            audit, _, _ = await self.gateway.call("auditor", self.prompts["audit"] + "\n" +
                                                 json.dumps({"source": source_data, "solution": plan.model_dump(),
                                                             "item": item.model_dump(),
                                                             "parent": data["parent"]}, ensure_ascii=False),
                                                 salt + ":audit", Audit)
            result = Candidate(id=identity, parent_id=parent.id if parent else None,
                               root_id=parent.root_id if parent else identity, generation=generation,
                               operator=operator, source_ids=[source.id], solution=plan, item=item, audit=audit,
                               blueprint_audit=blueprint, blueprint_request=bid, blueprint_actual_model=bmodel,
                               status="quality_seed", run_id=self.run_id)
            errors = static_errors(result, {source.id: source}, self.quotas)
            if (item.category, item.subtopic) != (target["category"], target["subtopic"]):
                errors.append("requested_category_mismatch")
            if not audit.passed():
                errors.append("audit_failed:" + audit.reason)
            if not errors:
                result.authoring_complete = False
                self.store.save(result)
                if await self.preflight(result):
                    result.authoring_complete = True
                    self.store.save(result)
                    return result
                errors.extend(result.reasons)
            result.status, result.reasons = "rejected", errors
            result.authoring_repairs_completed = repair+1
            result.authoring_complete = repair == self.settings.max_repairs
            self.store.save(result)
            self.log("repair_feedback", candidate=identity, repair=repair, reasons=errors)
            data["feedback"] = {"original": feedback, "errors": errors, "previous": item.model_dump()}
        return result

    async def sample(self, candidate, limit=None):
        if not candidate.authoring_complete:
            raise ValueError("Finish the pending author repair before paid solver evaluation")
        limit = self.settings.samples if limit is None else limit
        if not 1 <= limit <= self.settings.samples:
            raise ValueError("Invalid sampling stage")
        prior = list(candidate.trials)
        if (len({t.sample_id for t in prior}) != len(prior) or
                any(not 0 <= t.sample_id < self.settings.samples for t in prior)):
            raise ValueError("Invalid persisted sample identities")
        for role in ("solver", "judge"):
            if len({getattr(t, role + "_request") for t in prior}) != len(prior):
                raise ValueError("Reused persisted sample response")
        if not await self.preflight(candidate):
            raise QualityRejected("Question failed preflight; no difficulty samples allowed")
        done = {t.sample_id for t in prior}

        async def one(i):
            try:
                response, sid, smodel = await self.gateway.call(
                    "solver", candidate.item.question, f"{candidate.id}:solver:{i}",
                    system_prompt=self.prompts["solver_hle_system"].strip())
                judgment, jid, jmodel = await judge_answer(
                    self.gateway, self.prompts["judge"].format(question=candidate.item.question,
                                                               correct_answer=candidate.item.answer, response=response),
                    f"{candidate.id}:judge:{i}", self.settings.judge_schema_repairs)
            except ModelResponseError as error:
                self.log("sample_failed", candidate=candidate.id, sample=i,
                         request_id=error.request_id, reason=error.reason)
                return error
            trial = Trial(sample_id=i, response=response, solver_request=sid, judge_request=jid,
                          solver_actual_model=smodel, judge_actual_model=jmodel, judgment=judgment)
            candidate.trials = sorted(candidate.trials + [trial], key=lambda t: t.sample_id)
            self.store.save(candidate)  # Commit each paired result before waiting for slow siblings.
            self.log("sample_completed", candidate=candidate.id, sample=i, correct=judgment.correct)
            return trial
        results = await gather_cancel_on_error(*(one(i) for i in range(limit) if i not in done))
        errors = [x for x in results if isinstance(x, Exception)]
        if errors:
            # Partial samples remain quality seeds, cannot pass the band or seed another round in this run.
            candidate.reasons = ["infrastructure_or_schema_error:" + type(e).__name__ + ":" + str(e)[:150]
                                 for e in errors]
            self.store.save(candidate)
            raise SampleIncomplete(f"{len(errors)} samples failed; paired successes retained for resume")
        return correct_count([t for t in candidate.trials if t.sample_id < limit], limit)

    async def duplicate(self, candidate):
        refs = self.external + [Record(id=c.id, question=c.item.question, material_key=c.item.material_key)
                                for c in self.store.candidates()
                                if c.id != candidate.id and c.status in ("screened", "accepted")]
        for row in refs:
            if lexical_duplicate(row.question, candidate.item.question):
                return [row.id]
        # No approximate top-K shortcut: all supplied references are semantically checked in bounded batches.
        for start in range(0, len(refs), self.settings.semantic_batch_size):
            batch = refs[start:start + self.settings.semantic_batch_size]
            verdict, _, _ = await self.gateway.call(
                "auditor", self.prompts["dedup"] + "\n" + json.dumps({
                    "candidate": candidate.item.question, "references": [r.model_dump() for r in batch]},
                    ensure_ascii=False), f"{candidate.id}:dedup:{start}", DedupVerdict)
            if set(verdict.duplicate_ids) - {r.id for r in batch}:
                raise ValueError("Dedup returned unknown reference ids")
            if verdict.duplicate_ids:
                return verdict.duplicate_ids
        return []

    async def evaluate(self, c):
        if not c.authoring_complete:
            raise ValueError("Finish the pending author repair before paid solver evaluation")
        if not await self.preflight(c):
            return c
        if self.settings.early_screen_samples == 4 and len(c.trials) < self.settings.samples:
            first = await self.sample(c, 4)
            if first == 4:
                c.status, c.reasons = "quality_seed", ["preliminary_4_of_4_correct:needs_hardening"]
                self.store.save(c)
                self.log("preliminary_hardening", candidate=c.id, correct=4, valid_samples=4,
                         full_16_sample_verdict=False)
                return c
        n = await self.sample(c)
        self.log("difficulty", candidate=c.id, correct_count=n, total=self.settings.samples)
        c.reasons = []
        if self.settings.correct_min <= n <= self.settings.correct_max:
            duplicates = await self.duplicate(c)
            if duplicates:
                c.status, c.reasons = "rejected", ["duplicate:" + x for x in duplicates]
            else:
                c.status = "screened"
                if self.mode == "production":
                    c.reasons = can_count(c, self.store.candidates(), self.quotas, self.settings.max_per_root)
                    if not c.reasons:
                        c.status = "accepted"
                else:
                    c.reasons = ["pilot_only:not_counted_as_delivery"]
        else:
            c.reasons = ["outside_1_to_8_correct_band"]
        self.store.save(c)
        self.log("candidate_finished", candidate=c.id, status=c.status, reasons=c.reasons)
        return c

    def start(self, invocation=None):
        self.invocation = invocation
        self.fingerprint = digest({"base": self.base_fingerprint, "invocation": invocation})
        if self.mode == "production":
            blockers = production_blockers(self.settings, self.quotas)
            if blockers:
                raise ValueError("Production prerequisites: " + "; ".join(blockers))
        prior = self.store.runs().get(self.run_id)
        if prior and prior["fingerprint"] != self.fingerprint:
            raise ValueError("Run inputs/config/code changed; use a new --run-id")
        self.metadata("running")
        if hasattr(self.store, "save_quotas"):
            self.store.save_quotas(self.quotas)

    async def qc(self, submissions, *, preflight_only=False):
        """Quality control accepts authored bundles, never caller-provided scores or audit flags."""
        submissions = list(submissions)
        if not submissions or len({s.id for s in submissions}) != len(submissions):
            raise ValueError("Provide nonempty submissions with unique ids")
        self.start({"command": "preflight" if preflight_only else "qc",
                    "submissions_sha256": digest([s.model_dump() for s in submissions])})
        sources = {s.id: s for s in self.store.sources()}
        incomplete = False
        try:
            for submission in submissions:
                root_identity = digest(submission.model_dump())[:24]
                identity = digest({"run": self.run_id, "submission": submission.model_dump()})[:24]
                old = next((c for c in self.store.candidates() if c.id == identity), None)
                if old:
                    if old.run_id != self.run_id:
                        raise ValueError("Submission already exists in a different run; use original run to resume")
                    if old.status in ("accepted", "screened"):
                        continue
                    if old.status == "rejected":
                        continue
                    if preflight_only:
                        await self.preflight(old)
                    else:
                        try:
                            await self.evaluate(old)
                        except SampleIncomplete:
                            incomplete = True
                    continue
                parent = next((c for c in self.store.candidates() if c.id == submission.parent_id), None)
                if submission.parent_id and (parent is None or not await self.preflight(parent)):
                    raise ValueError("Unknown or unqualified parent")
                provisional_audit = Audit(valid=False, self_contained=False, uniquely_answerable=False,
                                           answer_supported=False, substantive_delta=False,
                                           category_correct=False, no_shortcut=False,
                                           reason="Not audited yet")
                c = Candidate(id=identity, parent_id=submission.parent_id,
                              root_id=parent.root_id if parent else root_identity,
                              generation=parent.generation+1 if parent else 0, operator="external_submission",
                              source_ids=sorted({ev.source_id for ev in submission.item.evidence +
                                                 submission.solution.evidence}),
                              solution=submission.solution, item=submission.item, audit=provisional_audit,
                              status="quality_seed", run_id=self.run_id)
                errors = static_errors(c, sources, self.quotas)
                if self.mode == "production" and any(sources[s].kind == "synthetic"
                                                       for s in c.source_ids if s in sources):
                    errors.append("synthetic_fixture")
                if errors:
                    c.status, c.reasons = "rejected", errors
                    self.store.save(c)
                    continue
                c.audit, _, _ = await self.gateway.call("auditor", self.prompts["audit"] + "\n" +
                    json.dumps({"item": c.item.model_dump(), "solution": c.solution.model_dump(),
                                "sources": [sources[s].model_dump() for s in c.source_ids],
                                "parent": parent.item.model_dump() if parent else None}, ensure_ascii=False),
                    f"{identity}:audit", Audit)
                if not c.audit.passed():
                    c.status, c.reasons = "rejected", ["audit_failed:" + c.audit.reason]
                    self.store.save(c)
                    continue
                self.store.save(c)
                if preflight_only:
                    await self.preflight(c)
                    continue
                try:
                    await self.evaluate(c)
                except SampleIncomplete:
                    incomplete = True
        except BaseException as error:
            self.metadata("budget_exhausted" if isinstance(error, BudgetExceeded) else "interrupted")
            raise
        self.metadata("completed_with_errors" if incomplete else "completed")
        return report(self.store, self.quotas)

    async def run(self, subtopic: str | None = None):
        rows = self.quotas["quotas"]
        if subtopic:
            rows = [q for q in rows if q["subtopic"] == subtopic]
            if not rows:
                raise ValueError("Unknown procurement subtopic")
        # Schedule by remaining per-subtopic deficit, with no cross-category offsetting.
        counts = Counter((c.item.category, c.item.subtopic) for c in self.store.candidates() if c.status == "accepted")
        rows = sorted(rows, key=lambda q: -(q["count"] - counts[(q["category"], q["subtopic"])]))
        sources = self.store.sources()
        if not sources:
            raise ValueError("Import full-text sources before running")
        if self.mode == "production" and any(s.kind == "synthetic" for s in sources):
            raise ValueError("Synthetic offline fixtures cannot enter production")
        routes = None if subtopic else read_routes(self.settings.source_routes_path, sources, self.quotas)
        if routes is not None and not any(routes["sources"][s.id]["subtopics"] for s in sources):
            raise ValueError("No source supports a procurement subtopic")
        self.start({"command": "run", "subtopic": subtopic, "routes_sha256": digest(routes)})
        incomplete = False
        try:
            for source in sources:
                generated = 0
                source_rows = rows if routes is None else [r for r in rows if
                    r["subtopic"] in routes["sources"][source.id]["subtopics"]]
                for target in source_rows:
                    if counts[(target["category"], target["subtopic"])] >= target["count"]:
                        continue
                    parent = None
                    for generation in range(self.settings.max_rounds):
                        if generated >= self.settings.source_max_candidates:
                            break
                        for attempt in range(self.settings.attempts_per_round):
                            if generated >= self.settings.source_max_candidates:
                                break
                            self.log("generation_started", source=source.id, subtopic=target["subtopic"],
                                     generation=generation, attempt=attempt)
                            try:
                                c = await self.generate(source, parent, target, generation, attempt)
                            except AuthoringRejected:
                                generated += 1
                                continue
                            generated += 1
                            if c.status == "rejected":
                                continue
                            if c.status in ("accepted", "screened"):
                                parent = c
                                continue
                            try:
                                await self.evaluate(c)
                            except SampleIncomplete:
                                incomplete = True
                                continue
                            if c.status == "accepted":
                                counts[(c.item.category, c.item.subtopic)] += 1
                            if c.status != "rejected":
                                parent = c
                            break
                        if parent is None:
                            break
                    if generated >= self.settings.source_max_candidates:
                        break
        except BaseException as error:
            self.metadata("budget_exhausted" if isinstance(error, BudgetExceeded) else "interrupted")
            raise
        self.metadata("completed_with_errors" if incomplete else "completed")
        return report(self.store, self.quotas)


def report(store, quotas):
    candidates = store.candidates()
    sources = {s.id: s for s in store.sources()}
    accepted = [c for c in candidates if c.status == "accepted"]
    counts = Counter((c.item.category, c.item.subtopic) for c in accepted)
    return {"target": quotas["target"], "accepted": len(accepted),
            "statuses": dict(Counter(c.status for c in candidates)),
            "candidates": [{"id": c.id, "parent_id": c.parent_id, "generation": c.generation,
                            "subtopic": c.item.subtopic, "status": c.status, "reasons": c.reasons,
                            "consistency_passed": has_valid_consistency(c, sources),
                            "difficulty_measurement_valid": (c.audit.passed()
                                and has_valid_consistency(c, sources) and len(c.trials) == 16),
                            "samples": len(c.trials),
                            "correct_count": correct_count(c.trials) if len(c.trials) == 16 else None}
                           for c in candidates],
            "remaining": [{**q, "accepted": counts[(q["category"], q["subtopic"])],
                           "remaining": max(0, q["count"] - counts[(q["category"], q["subtopic"])])}
                          for q in quotas["quotas"]]}
