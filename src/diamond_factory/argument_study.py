"""独立的模型论证评测研究合同；不改HLE模板、生产Trial或导出资格。"""
import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .evaluation import judge_answer
from .llm import ModelResponseError
from .models import DeadlineOutcome, Judgment, StrictModel, VerifierOracle, VerifierOracleCase, digest
from .pipeline import QualityRejected, SampleIncomplete
from .runtime import atomic_json
from .scientific_goals import argument_validation_required
from .verifier import VerifierBuilder

PROTOCOL = "model-argument-study-v1"
PROMPTS = ("argument_response", "argument_controls", "argument_oracle")


class OutputArgument(StrictModel):
    output_index: int = Field(ge=0)
    verdict: Literal["yes", "no", "uncertain"]
    reasoning: str = Field(min_length=20)
    witness: str = Field(min_length=20)


class ArgumentDecision(StrictModel):
    outputs: list[OutputArgument] = Field(min_length=1)

    def verdict(self):
        values = {c.verdict for c in self.outputs}
        return "no" if "no" in values else "uncertain" if "uncertain" in values else "yes"


class ArgumentControl(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    kind: Literal["equivalent", "omitted_argument", "invalid_argument"]
    response: str = Field(min_length=20, max_length=24000)


class ArgumentControls(StrictModel):
    cases: list[ArgumentControl] = Field(min_length=4, max_length=8)

    @model_validator(mode="after")
    def unique_cases(self):
        if (len({c.id for c in self.cases}) != len(self.cases) or any(c.id == "golden" for c in self.cases)
                or len({c.response for c in self.cases}) != len(self.cases)):
            raise ValueError("Distinct control identities and responses required; golden is reserved")
        return self


class ArgumentOracle(VerifierOracle):
    final_results: list[VerifierOracleCase]


class ResponseGrade(StrictModel):
    response: str
    hle: Judgment | None = None
    hle_request: str = ""
    hle_actual_model: str = ""
    argument: ArgumentDecision | None = None
    argument_request: str = ""
    argument_actual_model: str = ""

    def verdict(self):
        if not self.hle or not self.argument:
            return "pending"
        verdict = self.argument.verdict()
        if verdict == "uncertain" or (verdict == "yes" and self.hle.correct == "no"):
            return "unresolved"
        return verdict


class ArgumentSample(StrictModel):
    sample_id: int = Field(ge=0, le=15)
    solver_request: str = ""
    solver_actual_model: str = ""
    grade: ResponseGrade | None = None
    deadline: DeadlineOutcome | None = None

    def verdict(self):
        return "no" if self.deadline else self.grade.verdict() if self.grade else "pending"


class ArgumentStudyState(StrictModel):
    protocol: Literal["model-argument-study-v1"] = PROTOCOL
    fingerprint: str
    controls: ArgumentControls | None = None
    controls_request: str = ""
    oracle: ArgumentOracle | None = None
    oracle_request: str = ""
    postreview: ArgumentOracle | None = None
    postreview_request: str = ""
    control_grades: dict[str, ResponseGrade] = Field(default_factory=dict)
    samples: list[ArgumentSample] = Field(default_factory=list)
    phase: str = "pending"
    failures: list[dict] = Field(default_factory=list)


def mapping_valid(decision, count):
    ids = [o.output_index for o in decision.outputs]
    return len(ids) == count and set(ids) == set(range(count))


def control_errors(state, count):
    if state.controls is None or state.oracle is None:
        return ["missing_controls_or_oracle"]
    errors = []
    oracle = {o.id: o.verdict for o in state.oracle.cases}
    final = {o.id: o.verdict for o in state.oracle.final_results}
    expected = {"golden", *(c.id for c in state.controls.cases)}
    if len(oracle) != len(state.oracle.cases) or set(oracle) != expected or set(state.control_grades) != expected:
        errors.append("control_mapping_mismatch")
    if len(final) != len(state.oracle.final_results) or set(final) != expected or set(final.values()) != {"yes"}:
        errors.append("control_final_results_not_preserved")
    if any(getattr(state.oracle, k).verdict != "pass" for k in
           ("question_complete", "target_unambiguous", "golden_valid", "hidden_steps_recoverable")):
        errors.append("unresolved_question_or_gold")
    if oracle.get("golden") != "yes":
        errors.append("golden_not_validated")
    positive = [c for c in state.controls.cases if c.kind == "equivalent" and oracle.get(c.id) == "yes"]
    if len(positive) < 2:
        errors.append("insufficient_positive_controls")
    for kind in ("omitted_argument", "invalid_argument"):
        if not any(c.kind == kind and oracle.get(c.id) == "no" for c in state.controls.cases):
            errors.append("missing_proof_negative:" + kind)
    for key, grade in state.control_grades.items():
        if not grade.argument or not mapping_valid(grade.argument, count):
            errors.append("output_mapping:" + key)
        if oracle.get(key) not in ("yes", "no") or grade.verdict() != oracle.get(key):
            errors.append("control_disagreement:" + key)
    return errors


class ArgumentStudy:
    def __init__(self, pipeline, candidate):
        self.p, self.c = pipeline, candidate
        self._ready = False
        if candidate.known_result is None:
            raise ValueError("Argument study requires a locked scientific result")
        self.prompts = {k: (Path("prompts") / (k + ".txt")).read_text() for k in PROMPTS}
        sources = {s.id: s.sha256 for s in pipeline.store.sources() if s.id in candidate.source_ids}
        self.fingerprint = digest({"protocol": PROTOCOL, "item": candidate.item.model_dump(),
            "solution": candidate.solution.model_dump(), "known_result": candidate.known_result.model_dump(),
            "sources": sources, "settings": pipeline.settings.model_dump(), "pipeline": pipeline.fingerprint,
            "prompts": self.prompts, "code": Path(__file__).read_text()})
        self.state_run = pipeline.run_id + "-argument-state"
        saved = pipeline.store.runs().get(self.state_run, {})
        if "object_key" in saved:
            raw = pipeline.store.objects.get(saved["object_key"], saved["object_sha256"], saved["object_size"])
        else:
            raw = saved.get("state")
        self.state = ArgumentStudyState.model_validate(raw) if raw else ArgumentStudyState(fingerprint=self.fingerprint)
        if self.state.fingerprint != self.fingerprint:
            raise ValueError("Argument-study inputs/protocol changed: create a new run, do not reuse approvals")

    def save(self):
        raw = self.state.model_dump()
        if hasattr(self.p.store, "objects"):
            key, sha, size = self.p.store.objects.put(raw)
            body = {"object_key": key, "object_sha256": sha, "object_size": size}
        else:
            body = {"state": raw}  # 仅离线测试用SQLite；VM使用PG元数据和CAS。
        self.p.store.run(self.state_run, dict(mode=PROTOCOL, status=self.state.phase, **body))
        atomic_json(self.p.run_dir / "argument-study.json", raw)

    async def grade(self, grade, identity):
        async def argument():
            if grade.argument:
                return
            decision, rid, model = await self.p.consistency_call(self.prompts["argument_response"] + "\n" +
                json.dumps({"question": self.c.item.question, "required_outputs": self.c.item.required_outputs,
                            "response": grade.response}, ensure_ascii=False), identity + ":argument", ArgumentDecision)
            if not mapping_valid(decision, len(self.c.item.required_outputs)):
                raise ModelResponseError("consistency", rid, "invalid_argument_output_mapping")
            grade.argument, grade.argument_request, grade.argument_actual_model = decision, rid, model
            self.save()

        async def hle():
            if grade.hle:
                return
            grade.hle, grade.hle_request, grade.hle_actual_model = await judge_answer(self.p.gateway,
                self.p.prompts["judge"].format(question=self.c.item.question, correct_answer=self.c.item.answer,
                                             response=grade.response), identity + ":hle",
                self.p.settings.judge_schema_repairs, system_prompt=self.p.prompts["judge_system"])
            self.save()

        results = await asyncio.gather(argument(), hle(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def controls(self):
        state, p, c = self.state, self.p, self.c
        state.phase = "argument_controls"
        sources = {s.id: s for s in p.store.sources()}
        packet = VerifierBuilder(p).packet(c, sources)
        if not state.controls:
            state.controls, state.controls_request, _ = await p.consistency_call(self.prompts["argument_controls"] +
                "\n" + json.dumps(packet, ensure_ascii=False), c.id + ":argument-controls", ArgumentControls)
            if any(case.response == c.item.answer for case in state.controls.cases):
                raise ModelResponseError("consistency", state.controls_request, "duplicate_golden_argument_control")
            self.save()
        responses = {"golden": c.item.answer, **{r.id: r.response for r in state.controls.cases}}
        if not state.oracle:
            packet["responses"] = [{"id": k, "response": v} for k, v in responses.items()]
            state.oracle, state.oracle_request, _ = await p.consistency_call(self.prompts["argument_oracle"] + "\n" +
                json.dumps(packet, ensure_ascii=False), c.id + ":argument-control-oracle", ArgumentOracle)
            self.save()
        for key, response in responses.items():
            state.control_grades.setdefault(key, ResponseGrade(response=response))
        self.save()
        results = await asyncio.gather(*(self.grade(state.control_grades[key], c.id + ":argument-control:" + key)
                                         for key in responses), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        errors = control_errors(state, len(c.item.required_outputs))
        p.log("argument_control_results", candidate=c.id, errors=errors,
            hle_only_false_accepts=[k for k, g in state.control_grades.items()
                                   if g.hle and g.hle.correct == "yes" and g.argument and g.argument.verdict() == "no"])
        if errors:
            raise QualityRejected("argument_controls_failed:" + ";".join(errors))

    async def _sample(self, i):
        if (not self._ready or control_errors(self.state, len(self.c.item.required_outputs))
                or ArgumentStudy(self.p, self.c).fingerprint != self.fingerprint):
            raise QualityRejected("Scientific/argument controls must pass before each solver request")
        row = next((r for r in self.state.samples if r.sample_id == i), None)
        if row is None:
            row = ArgumentSample(sample_id=i)
            self.state.samples.append(row)
        if row.deadline:
            verified = self.p.gateway.deadline_outcome(i, self.c.item.question,
                f"{self.c.id}:solver:{i}", row.solver_request,
                system_prompt=self.p.prompts["solver_hle_system"].strip())
            if row.deadline != verified:
                raise ValueError("Argument study deadline receipt changed")
            return
        if row.grade is None:
            try:
                response, rid, model = await self.p.gateway.call("solver", self.c.item.question,
                    f"{self.c.id}:solver:{i}", system_prompt=self.p.prompts["solver_hle_system"].strip())
            except ModelResponseError as error:
                if error.reason != "deadline_exceeded" or not self.p.settings.solver_deadline_counts_as_incorrect:
                    raise
                row.solver_request = error.request_id
                row.deadline = self.p.gateway.deadline_outcome(i, self.c.item.question,
                    f"{self.c.id}:solver:{i}", error.request_id,
                    system_prompt=self.p.prompts["solver_hle_system"].strip())
                self.save()
                return
            row.solver_request, row.solver_actual_model, row.grade = rid, model, ResponseGrade(response=response)
            self.save()  # 解题成功即落盘；judge失败不丢答卷或重购。
        await self.grade(row.grade, f"{self.c.id}:argument-sample:{i}")
        self.p.log("argument_sample_completed", candidate=self.c.id, sample=i, verdict=row.verdict())

    async def run(self):
        p, c, state = self.p, self.c, self.state
        if not (p.settings.require_verifier_adversary and p.settings.freeze_question_at_verifier
                and p.settings.solver_deadline_counts_as_incorrect and argument_validation_required(c)
                and c.authoring_complete and p.settings.samples == 16 and p.settings.early_screen_samples == 4):
            raise ValueError("Explicit frozen proof study with complete authoring and 4→16 policy required")
        if not await p.preflight(c):
            raise QualityRejected("Scientific preflight failed; no argument study solver request allowed")
        if self.fingerprint != ArgumentStudy(p, c).fingerprint:
            raise ValueError("Scientific revalidation changed the frozen study inputs; prepare a new run")
        try:
            await self.controls()
            if control_errors(state, len(c.item.required_outputs)):
                raise QualityRejected("Missing validated argument controls")
            self._ready = True
            for limit in (4, 16):
                state.phase = "screening_first4" if limit == 4 else "supplementing"
                self.save()
                results = await asyncio.gather(*(self._sample(i) for i in range(limit)), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                verdicts = [r.verdict() for r in state.samples if r.sample_id < limit]
                if len(verdicts) != limit or any(v not in ("yes", "no") for v in verdicts):
                    raise SampleIncomplete("Argument judge uncertainty/disagreement is not a wrong answer")
                correct = verdicts.count("yes")
                if limit == 4 and correct == 4:
                    state.phase = "too_easy_first4"
                    break
                if limit == 16:
                    state.phase = "zero_correct_needs_science_review" if correct == 0 else (
                        "in_band_needs_postreview" if correct <= 8 else "too_easy_16")
                    if correct <= 8:
                        sources = {s.id: s for s in p.store.sources()}
                        packet = VerifierBuilder(p).packet(c, sources)
                        packet["responses"] = [{"id": "golden", "response": c.item.answer}] + [
                            {"id": "sample_" + str(r.sample_id), "response": r.grade.response}
                            for r in state.samples if r.grade]
                        if not state.postreview:
                            state.postreview, state.postreview_request, _ = await p.consistency_call(
                                self.prompts["argument_oracle"] + "\n" + json.dumps(packet, ensure_ascii=False),
                                c.id + ":argument-postreview", ArgumentOracle)
                            self.save()
                        truth = {r.id: r.verdict for r in state.postreview.cases}
                        expected = {r["id"] for r in packet["responses"]}
                        sound = (len(truth) == len(state.postreview.cases) and set(truth) == expected
                            and truth.get("golden") == "yes" and all(getattr(state.postreview, k).verdict == "pass"
                            for k in ("question_complete", "target_unambiguous", "golden_valid", "hidden_steps_recoverable"))
                            and all(truth.get("sample_" + str(r.sample_id)) == r.verdict()
                                    for r in state.samples if r.grade))
                        if not sound:
                            raise SampleIncomplete("Argument postreview has a scientific/grading dispute; preserve all samples")
                        state.phase = "zero_correct_science_review_passed" if correct == 0 else "in_band_postreview_passed"
            self.save()
            return self.report()
        except BaseException as error:
            state.failures.append({"kind": type(error).__name__, "reason": str(error)[:500]})
            state.phase = "incomplete"
            self.save()
            raise

    def report(self):
        rows = self.state.samples
        return {"protocol": PROTOCOL, "candidate_id": self.c.id, "planned_questions": 1,
            "state": self.state.phase, "actual_answers": sum(r.grade is not None for r in rows),
            "deadline_incorrect": sum(r.deadline is not None for r in rows),
            "correct": sum(r.verdict() == "yes" for r in rows), "incorrect": sum(r.verdict() == "no" for r in rows),
            "pending_or_unresolved": sum(r.verdict() not in ("yes", "no") for r in rows),
            "hle_final_match": sum(bool(r.grade and r.grade.hle and r.grade.hle.correct == "yes") for r in rows),
            "production_deliveries": 0, "formal_proof_certification": False}
