"""前置verifier的有限对抗；所有模型意见与执行记录均可证伪、可恢复。"""
import json
from pathlib import Path

from .evaluation import judge_answer
from .llm import ModelResponseError
from .models import (
    VerifierAssessment,
    VerifierChallenges,
    VerifierDecision,
    VerifierObservation,
    VerifierOracle,
    VerifierQuestionRepair,
    VerifierReview,
    VerifierRound,
    VerifierSpec,
    VerifierVersion,
    digest,
)
from .runtime import gather_cancel_on_error

PROMPTS = ("verifier_draft", "verifier_challenge", "verifier_oracle", "verifier_apply",
           "verifier_review", "verifier_question_repair", "judge", "judge_system", "audit",
           "consistency_solve", "consistency_review", "consistency_certificate", "bottleneck_audit")


def context_hash(candidate, sources):
    # 题面版本另存；外部证明/来源变化不能挪用旧对抗检查点。
    return digest({"solution": candidate.solution.model_dump(),
                   "known_result": candidate.known_result.model_dump() if candidate.known_result else None,
                   "source_hashes": {sid: sources[sid].sha256 for sid in candidate.source_ids}})


def protocol_hash(prompts, settings):
    return digest({"prompts": {name: digest(prompts[name]) for name in PROMPTS},
                   "models": {r: getattr(settings, r).model_dump() for r in ("generator", "consistency", "judge")},
                   "max_revisions": settings.verifier_max_revisions,
                   "code": Path(__file__).read_text()})


def spec_errors(spec, item):
    targets = [r.target_output for r in spec.requirements]
    errors = []
    if sorted(targets) != sorted(item.required_outputs) or len(set(targets)) != len(targets):
        errors.append("verifier_output_coverage_mismatch")
    if any(r.question_anchor not in item.question for r in [*spec.requirements, *spec.conditions]):
        errors.append("verifier_condition_or_output_not_anchored")
    return errors


def round_errors(result, item):
    """不相信模型的总pass：程序核对控制、覆盖、误杀/漏检及HLE兼容。"""
    errors = []
    oracle = result.oracle
    if any(getattr(oracle, name).verdict != "pass" for name in
           ("question_complete", "golden_valid", "hidden_steps_recoverable")):
        errors.append("unresolved_question_or_golden")
    cases = {c.id: c for c in result.challenges.cases}
    outcomes = {o.case_id: o for o in result.observations}
    if set(outcomes) != {"golden", *cases} or len(outcomes) != len(result.observations):
        errors.append("missing_or_duplicate_observations")
    oracle_cases = {o.id: o for o in oracle.cases}
    if (len(oracle_cases) != len(oracle.cases) or set(oracle_cases) != set(outcomes)
            or any(o.oracle != oracle_cases.get(o.case_id) for o in result.observations)):
        errors.append("oracle_observation_mapping_mismatch")
    positive = [o for o in result.observations if o.oracle.verdict == "yes" and o.case_id != "golden"]
    negative = [o for o in result.observations if o.oracle.verdict == "no"]
    if len(positive) < 2 or len(negative) < 2:
        errors.append("insufficient_validated_controls")
    covered = {cases[o.case_id].target_output for o in negative if o.case_id in cases}
    if covered != set(item.required_outputs):
        errors.append("negative_output_coverage_incomplete")
    golden = outcomes.get("golden")
    if not golden or golden.oracle.verdict != "yes":
        errors.append("golden_not_validated")
    for o in result.observations:
        if o.oracle.verdict == "uncertain" or o.verifier.correct == "uncertain":
            errors.append("unresolved_control:" + o.case_id)
        elif o.verifier.correct != o.oracle.verdict:
            errors.append(("false_rejection:" if o.oracle.verdict == "yes" else "false_acceptance:") + o.case_id)
        if o.oracle.verdict != o.hle.correct:
            errors.append("hle_disagreement:" + o.case_id)
    review = result.review
    if any(getattr(review, name).verdict != "pass" for name in
           ("question_verifier_consistent", "requested_outputs_covered", "no_hidden_extra_conditions")):
        errors.append("verifier_semantic_review_failed")
    if review.action != "pass":
        errors.append("review_requested:" + review.action)
    return errors


def has_valid_verifier(candidate, sources, *, expected_protocol=None):
    assessment = candidate.verifier_assessment
    if not assessment or assessment.status != "passed" or assessment.context_sha256 != context_hash(candidate, sources):
        return False
    if expected_protocol and assessment.protocol_sha256 != expected_protocol:
        return False
    final = assessment.versions[-1]
    return (not final.revalidation_pending and final.item == candidate.item and final.result is not None
            and not spec_errors(final.spec, candidate.item) and not round_errors(final.result, candidate.item))


class VerifierBuilder:
    def __init__(self, pipeline):
        self.p = pipeline

    def packet(self, c, sources):
        return {"item": c.item.model_dump(), "initial_golden": {
                    "known_result": c.known_result.model_dump() if c.known_result else None,
                    "solution": c.solution.model_dump(),
                    "blueprint_review": c.blueprint_audit.model_dump() if c.blueprint_audit else None},
                "sources": [{**sources[s].model_dump(), "text": sources[s].text[:self.p.settings.max_source_chars]}
                            for s in c.source_ids]}

    async def draft(self, c, sources, index, feedback=None):
        packet = self.packet(c, sources)
        packet["feedback"] = feedback
        spec, rid, model = await self.p.gateway.call("generator", self.p.prompts["verifier_draft"] + "\n" +
            json.dumps(packet, ensure_ascii=False), self.salt(c, index, "draft"), VerifierSpec)
        if spec_errors(spec, c.item):
            raise ModelResponseError("generator", rid, "invalid_verifier_coverage_or_anchor")
        return VerifierVersion(item=c.item.model_copy(deep=True), spec=spec, spec_request=rid, spec_actual_model=model)

    def salt(self, c, index, stage):
        return f"{c.id}:verifier:{c.authoring_repairs_completed}:{digest(c.item.model_dump())}:{index}:{stage}"

    async def test(self, c, version, sources, index):
        p = self.p
        packet = self.packet(c, sources)
        packet["verifier"] = version.spec.model_dump()
        challenges, cid, cm = await p.consistency_call(p.prompts["verifier_challenge"] + "\n" +
            json.dumps(packet, ensure_ascii=False), self.salt(c, index, "challenge"), VerifierChallenges)
        if any(case.target_output not in c.item.required_outputs or case.response == c.item.answer
               for case in challenges.cases):
            raise ModelResponseError("consistency", cid, "invalid_verifier_challenges")
        responses = [{"id": "golden", "response": c.item.answer}] + [
            {"id": case.id, "response": case.response} for case in challenges.cases]
        oracle_packet = self.packet(c, sources)
        # 刻意不传挑战者标签、规则和任何判分结果。
        oracle_packet["responses"] = responses
        oracle, oid, om = await p.consistency_call(p.prompts["verifier_oracle"] + "\n" +
            json.dumps(oracle_packet, ensure_ascii=False), self.salt(c, index, "oracle"), VerifierOracle)
        by_id = {o.id: o for o in oracle.cases}
        if len(by_id) != len(oracle.cases) or set(by_id) != {r["id"] for r in responses}:
            raise ModelResponseError("consistency", oid, "invalid_verifier_oracle_case_mapping")

        async def one(response):
            verdict, vid, vm = await p.consistency_call(p.prompts["verifier_apply"] + "\n" + json.dumps({
                "question": c.item.question, "golden": c.item.answer, "verifier": version.spec.model_dump(),
                "response": response["response"]}, ensure_ascii=False),
                self.salt(c, index, "apply:" + response["id"]), VerifierDecision)
            hle, hid, hm = await judge_answer(p.gateway, p.prompts["judge"].format(question=c.item.question,
                correct_answer=c.item.answer, response=response["response"]),
                self.salt(c, index, "hle-control:" + response["id"]), p.settings.judge_schema_repairs,
                system_prompt=p.prompts["judge_system"])
            return VerifierObservation(case_id=response["id"], oracle=by_id[response["id"]], verifier=verdict,
                verifier_request=vid, verifier_actual_model=vm, hle=hle, hle_request=hid, hle_actual_model=hm)

        observations = await gather_cancel_on_error(*(one(r) for r in responses))
        review_packet = {**packet, "challenges": challenges.model_dump(), "oracle": oracle.model_dump(),
                         "observations": [o.model_dump() for o in observations]}
        review, rid, rm = await p.consistency_call(p.prompts["verifier_review"] + "\n" +
            json.dumps(review_packet, ensure_ascii=False), self.salt(c, index, "review"), VerifierReview)
        return VerifierRound(challenges=challenges, oracle=oracle, observations=observations, review=review,
            requests={"challenge": cid, "oracle": oid, "review": rid},
            actual_models={"challenge": cm, "oracle": om, "review": rm})

    def save(self, c):
        self.p.store.save(c)

    def reject(self, c, reason):
        c.verifier_assessment.status, c.verifier_assessment.reason = "rejected", reason
        c.status, c.reasons = "rejected", ["verifier_adversary:" + reason]
        self.save(c)
        return False

    async def run(self, c, sources, revalidate):
        p = self.p
        protocol = protocol_hash(p.prompts, p.settings)
        if has_valid_verifier(c, sources, expected_protocol=protocol):
            return True
        if c.trials or c.deadline_outcomes:
            # 绝不清空已付费样本后原地改题，历史题必须新建版本。
            c.status, c.reasons = "rejected", ["verifier_adversary:measured_item_requires_new_version"]
            self.save(c)
            return False
        assessment = c.verifier_assessment
        if assessment and (assessment.context_sha256 != context_hash(c, sources)
                           or assessment.protocol_sha256 != protocol or assessment.versions[-1].item != c.item):
            return self.reject(c, "changed_context_requires_new_authoring_version")
        if not assessment:
            version = await self.draft(c, sources, 0)
            assessment = VerifierAssessment(context_sha256=context_hash(c, sources), protocol_sha256=protocol,
                                            versions=[version])
            c.verifier_assessment = assessment
            self.save(c)
        if assessment.status == "rejected":
            return False
        while True:
            version = assessment.versions[-1]
            index = len(assessment.versions) - 1
            if version.revalidation_pending:
                if not await revalidate(c):
                    return self.reject(c, "question_repair_failed_full_correctness_review")
                version.revalidation_pending = False
                self.save(c)
            if version.result is None:
                version.result = await self.test(c, version, sources, index)
                self.save(c)
            result = version.result
            errors = round_errors(result, c.item)
            p.log("verifier_adversarial_round", candidate=c.id, revision=index, errors=errors,
                  action=result.review.action, item_sha256=digest(c.item.model_dump()))
            if not errors:
                assessment.status, assessment.reason = "passed", "All controls and independent semantic checks passed"
                self.save(c)
                return True
            if result.oracle.golden_valid.verdict != "pass":
                return self.reject(c, "invalid_or_uncertain_golden")
            if index >= p.settings.verifier_max_revisions:
                return self.reject(c, "two_revision_limit_or_configured_limit:" + ";".join(errors))
            action = result.review.action
            question_issue = (result.oracle.question_complete.verdict == "fail"
                              or result.oracle.hidden_steps_recoverable.verdict == "fail")
            if action == "repair_question" and not question_issue:
                return self.reject(c, "question_repair_without_concrete_question_defect")
            if action == "repair_verifier" and any(e.startswith("hle_disagreement:") for e in errors):
                return self.reject(c, "hle_disagreement_requires_grading_fix_or_question_clarification")
            if action not in ("repair_verifier", "repair_question"):
                return self.reject(c, "unresolved_adversarial_review:" + ";".join(errors))
            feedback = {"previous_verifier": version.spec.model_dump(), "round": result.model_dump(), "errors": errors}
            if action == "repair_question":
                patch, qid, qm = await p.gateway.call("generator", p.prompts["verifier_question_repair"] + "\n" +
                    json.dumps({**self.packet(c, sources), "feedback": feedback}, ensure_ascii=False),
                    self.salt(c, index, "repair-question"), VerifierQuestionRepair)
                updated = c.item.model_copy(update={"question": patch.question, "assumptions": patch.assumptions,
                                                   "rationale": patch.rationale})
                if updated == c.item:
                    return self.reject(c, "question_repair_made_no_change")
                # draft先做完再写入新题面；中断可依旧题收据重放，不跳过复核。
                proposed = c.model_copy(deep=True)
                proposed.item = updated
                next_version = await self.draft(proposed, sources, index + 1, feedback)
                next_version.revalidation_pending = True
                next_version.question_repair_request, next_version.question_repair_actual_model = qid, qm
                assessment.versions.append(next_version)
                c.item, c.consistency, c.bottleneck_assessment = updated, None, None
                c.status, c.reasons = "quality_seed", []
            else:
                assessment.versions.append(await self.draft(c, sources, index + 1, feedback))
            self.save(c)
