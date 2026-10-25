"""前置verifier的有限对抗；所有模型意见与执行记录均可证伪、可恢复。"""
import json
import re
from pathlib import Path

from .evaluation import judge_answer
from .llm import ModelResponseError
from .models import (
    VerifierAnchorRepair,
    VerifierAssessment,
    VerifierChallenges,
    VerifierControlAttempt,
    VerifierDecision,
    VerifierLocatorPatches,
    VerifierObservation,
    VerifierOracle,
    VerifierQuestionRepair,
    VerifierReview,
    VerifierRound,
    VerifierSpec,
    VerifierVersion,
    digest,
)
from .provenance import match_quote
from .runtime import gather_cancel_on_error

PROMPTS = ("verifier_draft", "verifier_challenge", "verifier_oracle", "verifier_apply",
           "verifier_anchors",
           "verifier_review", "verifier_question_repair", "judge", "judge_system", "audit",
           "consistency_solve", "consistency_review", "consistency_certificate", "consistency_certificate_anchors",
           "consistency_certificate_repair",
           "bottleneck_audit")


def context_hash(candidate, sources):
    # 题面版本另存；外部证明/来源变化不能挪用旧对抗检查点。
    data = {"solution": candidate.solution.model_dump(),
                   "known_result": candidate.known_result.model_dump() if candidate.known_result else None,
                   "source_hashes": {sid: sources[sid].sha256 for sid in candidate.source_ids}}
    if candidate.source_fidelity != "strict":
        data["source_fidelity"] = candidate.source_fidelity
    return digest(data)


def protocol_hash(prompts, settings):
    return digest({"prompts": {name: digest(prompts[name]) for name in PROMPTS},
                   "models": {r: getattr(settings, r).model_dump() for r in ("generator", "consistency", "judge")},
                   "max_revisions": settings.verifier_max_revisions,
                   "freeze_question_at_verifier": settings.freeze_question_at_verifier,
                   "source_fidelity": settings.source_fidelity,
                   "code": Path(__file__).read_text(),
                   "provenance_code": Path(__file__).with_name("provenance.py").read_text()})


def repair_spec_anchors(spec, item):
    """仅把定位引文投影为题面原文；规则/条件/金标不变，后续对抗仍必需。

    来源引文不允许语法改写。verifier定位另容忍英文冠词首字母及
    is a/be a的语法差异，不据此认可条件的真值或可推出性。
    """
    repaired, receipts = spec.model_copy(deep=True), []
    for group in ("requirements", "conditions"):
        for index, row in enumerate(getattr(repaired, group)):
            original = row.question_anchor
            if original in item.question:
                continue
            match = match_quote(original, item.question, unique=True)
            method = "presentation"
            if match is None:
                variants = [re.sub(r"^(A|An|The)(?= [a-z]{3})", lambda m: m[0].lower(), original)]
                if " is a " in original:
                    variants.append(original.replace(" is a ", " be a ", 1))
                matches = [m for v in variants if v != original
                           and (m := match_quote(v, item.question, unique=True))]
                spans = {(m.start, m.end) for m in matches}
                if len(spans) != 1:
                    continue
                match, method = matches[0], "locator_grammar"
            # 定位必须唯一；不能靠选择某个近似命中来附会条件。
            if len(match.literal) < 5 or item.question.count(match.literal) != 1:
                continue
            row.question_anchor = match.literal
            receipts.append(VerifierAnchorRepair(group=group, index=index, original=original,
                literal=match.literal, start=match.start, end=match.end, method=method))
    return repaired, receipts


def anchor_repairs_valid(version):
    if version.anchor_repairs and not version.spec_raw_sha256:
        return False
    raw = version.spec.model_copy(deep=True)
    try:
        for receipt in version.anchor_repairs:
            row = getattr(raw, receipt.group)[receipt.index]
            if row.question_anchor != receipt.literal:
                return False
            row.question_anchor = receipt.original
        if version.spec_raw_sha256 and digest(raw.model_dump()) != version.spec_raw_sha256:
            return False
        expected, receipts = repair_spec_anchors(raw, version.item)
        if receipts != version.anchor_repairs[:len(receipts)]:
            return False
        for receipt in version.anchor_repairs[len(receipts):]:
            row = getattr(expected, receipt.group)[receipt.index]
            if (receipt.method != "model_literal_locator" or not receipt.request_id
                    or row.question_anchor != receipt.original or len(receipt.literal) < 5
                    or receipt.original in version.item.question
                    or version.item.question[receipt.start:receipt.end] != receipt.literal
                    or version.item.question.count(receipt.literal) != 1):
                return False
            row.question_anchor = receipt.literal
        return expected == version.spec
    except IndexError:
        return False


def spec_errors(spec, item):
    targets = [r.target_output for r in spec.requirements]
    errors = []
    if sorted(targets) != sorted(item.required_outputs) or len(set(targets)) != len(targets):
        errors.append("verifier_output_coverage_mismatch")
    if any(r.question_anchor not in item.question for r in [*spec.requirements, *spec.conditions]):
        errors.append("verifier_condition_or_output_not_anchored")
    return errors


def control_coverage_errors(challenges, oracle, item):
    """只按独立oracle核定的对照计数；作者标签不提供通过资格。"""
    ids = {c.id: c for c in challenges.cases}
    cases = {c.id: c for c in oracle.cases}
    if len(cases) != len(oracle.cases) or set(cases) != {"golden", *ids}:
        return ["oracle_control_mapping_mismatch"]
    errors = []
    if sum(c.verdict == "yes" for name, c in cases.items() if name != "golden") < 2:
        errors.append("insufficient_positive_controls")
    negative = [ids[name] for name, c in cases.items() if c.verdict == "no" and name in ids]
    if len(negative) < 2:
        errors.append("insufficient_negative_controls")
    if {c.target_output_index for c in negative} != set(range(len(item.required_outputs))):
        errors.append("negative_output_coverage_incomplete")
    return errors


def round_errors(result, item):
    """不相信模型的总pass：程序核对控制、覆盖、误杀/漏检及HLE兼容。"""
    errors = []
    if result.control_attempts:
        previous = None
        for attempt in result.control_attempts:
            if attempt.coverage_errors != control_coverage_errors(attempt.challenges, attempt.oracle, item):
                errors.append("control_repair_receipt_mismatch")
            if previous:
                before = previous.challenges.cases
                original = {c.id: c.verdict for c in previous.oracle.cases}
                if (attempt.challenges.cases[:len(before)] != before
                        or any(original.get(c.id, c.verdict) != c.verdict for c in attempt.oracle.cases)):
                    errors.append("control_repair_changed_previous_case_or_oracle")
            previous = attempt
        if previous.challenges != result.challenges or previous.oracle != result.oracle:
            errors.append("control_repair_final_mapping_mismatch")
    oracle = result.oracle
    if any(getattr(oracle, name).verdict != "pass" for name in
           ("question_complete", "target_unambiguous", "golden_valid", "hidden_steps_recoverable")):
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
    covered = {cases[o.case_id].target_output_index for o in negative if o.case_id in cases}
    if covered != set(range(len(item.required_outputs))):
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
    if assessment.frozen_item_sha256 and (assessment.frozen_item_sha256 != digest(candidate.item.model_dump())
            or any(digest(v.item.model_dump()) != assessment.frozen_item_sha256 for v in assessment.versions)):
        return False
    return (not final.revalidation_pending and final.item == candidate.item and final.result is not None
            and anchor_repairs_valid(final) and not spec_errors(final.spec, candidate.item)
            and not round_errors(final.result, candidate.item))


class VerifierBuilder:
    def __init__(self, pipeline):
        self.p = pipeline

    def packet(self, c, sources):
        return {"item": c.item.model_dump(), "source_fidelity": c.source_fidelity, "initial_golden": {
                    "known_result": c.known_result.model_dump() if c.known_result else None,
                    "solution": c.solution.model_dump(),
                    "blueprint_review": c.blueprint_audit.model_dump() if c.blueprint_audit else None},
                "mutation_policy": ("FROZEN: question, golden and full proof cannot change. Repair verifier "
                    "or controls only. A question/proof defect must reject this version with upstream feedback."
                    if self.p.settings.freeze_question_at_verifier else "Legacy question repair protocol"),
                "sources": [{**sources[s].model_dump(), "text": sources[s].text[:self.p.settings.max_source_chars]}
                            for s in c.source_ids]}

    async def draft(self, c, sources, index, feedback=None):
        packet = self.packet(c, sources)
        packet["feedback"] = feedback
        for repair in range(self.p.settings.consistency_schema_repairs + 1):
            stage = "draft" if repair == 0 else f"draft-anchor-repair:{repair}"
            seed = self.p.authoring_seeds.get(c.origin_seed_sha256)
            if (index == 0 and repair == 0 and c.authoring_repairs_completed == 0 and seed
                    and seed.verifier_spec and seed.item == c.item and seed.solution == c.solution):
                spec, rid, model = seed.verifier_spec.model_copy(deep=True), seed.verifier_request, seed.verifier_actual_model
                self.p.log("verifier_draft_seed_reused", candidate=c.id, request_id=rid, scientific_pass=False)
            else:
                spec, rid, model = await self.p.gateway.call("generator", self.p.prompts["verifier_draft"] + "\n" +
                    json.dumps(packet, ensure_ascii=False), self.salt(c, index, stage), VerifierSpec)
            raw_sha256 = digest(spec.model_dump())
            spec, anchor_repairs = repair_spec_anchors(spec, c.item)
            errors = spec_errors(spec, c.item)
            if not errors:
                return VerifierVersion(item=c.item.model_copy(deep=True), spec=spec,
                    spec_request=rid, spec_actual_model=model, spec_raw_sha256=raw_sha256,
                    anchor_repairs=anchor_repairs)
            if errors == ["verifier_condition_or_output_not_anchored"] and repair < self.p.settings.consistency_schema_repairs:
                # 只修引用位置，不让模型重写已经形成的验收规则或重新决定gold。
                bad = [(g, i) for g in ("requirements", "conditions")
                       for i, row in enumerate(getattr(spec, g)) if row.question_anchor not in c.item.question]
                patches, aid, _ = await self.p.consistency_call(self.p.prompts["verifier_anchors"] + "\n" +
                    json.dumps({"question": c.item.question, "verifier": spec.model_dump(),
                                "invalid_locator_indices": bad}, ensure_ascii=False),
                    self.salt(c, index, f"literal-locators:{repair}"), VerifierLocatorPatches)
                if sorted((x.group, x.index) for x in patches.patches) != sorted(bad):
                    raise ModelResponseError("consistency", aid, "invalid_verifier_locator_mapping")
                for patch in patches.patches:
                    literal = patch.question_anchor
                    if c.item.question.count(literal) != 1:
                        raise ModelResponseError("consistency", aid, "nonliteral_or_ambiguous_verifier_locator")
                    row = getattr(spec, patch.group)[patch.index]
                    start = c.item.question.index(literal)
                    anchor_repairs.append(VerifierAnchorRepair(group=patch.group, index=patch.index,
                        original=row.question_anchor, literal=literal, start=start, end=start+len(literal),
                        method="model_literal_locator", request_id=aid))
                    row.question_anchor = literal
                return VerifierVersion(item=c.item.model_copy(deep=True), spec=spec, spec_request=rid,
                    spec_actual_model=model, spec_raw_sha256=raw_sha256, anchor_repairs=anchor_repairs)
            if repair == self.p.settings.consistency_schema_repairs:
                raise ModelResponseError("generator", rid, "invalid_verifier_coverage_or_anchor")
            # 原输出已有收据，续跑只读复用；锚点格式修复不得改数学验收语义。
            packet["format_repair"] = {"errors": errors, "untrusted_previous": spec.model_dump(),
                "exact_required_outputs": c.item.required_outputs,
                "instruction": "FORMAT REPAIR ONLY: copy target_output exactly from required_outputs and "
                    "question_anchor exactly from the literal question. Do not change any acceptance semantics, "
                    "question or golden. Return the complete verifier."}
        raise AssertionError("Unreachable bounded anchor repair")

    def salt(self, c, index, stage):
        return f"{c.id}:verifier:{c.authoring_repairs_completed}:{digest(c.item.model_dump())}:{index}:{stage}"

    async def test(self, c, version, sources, index):
        p = self.p
        packet = self.packet(c, sources)
        packet["verifier"] = version.spec.model_dump()
        packet["output_index_map"] = dict(enumerate(c.item.required_outputs))
        attempts = []
        for repair in range(p.settings.consistency_schema_repairs + 1):
            suffix = "" if repair == 0 else f"-controls-repair:{repair}"
            challenges, cid, cm = await p.consistency_call(p.prompts["verifier_challenge"] + "\n" +
                json.dumps(packet, ensure_ascii=False), self.salt(c, index, "challenge" + suffix), VerifierChallenges)
            if any(case.target_output_index >= len(c.item.required_outputs) or case.response == c.item.answer
                   for case in challenges.cases):
                raise ModelResponseError("consistency", cid, "invalid_verifier_challenges")
            if attempts and challenges.cases[:len(attempts[-1].challenges.cases)] != attempts[-1].challenges.cases:
                raise ModelResponseError("consistency", cid, "control_repair_changed_existing_case")
            responses = [{"id": "golden", "response": c.item.answer}] + [
                {"id": case.id, "response": case.response} for case in challenges.cases]
            oracle_packet = self.packet(c, sources)
            # 刻意不传挑战者标签、规则、前次oracle或任何判分结果。
            oracle_packet["responses"] = responses
            oracle, oid, om = await p.consistency_call(p.prompts["verifier_oracle"] + "\n" +
                json.dumps(oracle_packet, ensure_ascii=False), self.salt(c, index, "oracle" + suffix), VerifierOracle)
            by_id = {o.id: o for o in oracle.cases}
            if len(by_id) != len(oracle.cases) or set(by_id) != {r["id"] for r in responses}:
                raise ModelResponseError("consistency", oid, "invalid_verifier_oracle_case_mapping")
            if attempts and any(by_id[o.id].verdict != o.verdict for o in attempts[-1].oracle.cases):
                raise ModelResponseError("consistency", oid, "control_repair_changed_prior_oracle_verdict")
            errors = control_coverage_errors(challenges, oracle, c.item)
            attempts.append(VerifierControlAttempt(challenges=challenges, oracle=oracle,
                challenge_request=cid, oracle_request=oid, challenge_actual_model=cm, oracle_actual_model=om,
                coverage_errors=errors))
            sound = all(getattr(oracle, k).verdict == "pass" for k in
                        ("question_complete", "target_unambiguous", "golden_valid", "hidden_steps_recoverable"))
            if not errors or not sound or len(challenges.cases) >= 12 or repair == p.settings.consistency_schema_repairs:
                break
            p.log("verifier_control_suite_repair", candidate=c.id, revision=index, repair=repair,
                  errors=errors, challenge_request=cid, oracle_request=oid, question_changed=False)
            packet["control_generation_feedback"] = {"errors": errors,
                "previous_challenges": challenges.model_dump(), "oracle": oracle.model_dump(),
                "instruction": "APPEND controls only. Preserve every previous case, id, label, response, "
                    "index and rationale exactly and in order. Add complete genuinely valid equivalent "
                    "answers or invalid near misses needed for coverage. No question, gold or verifier changes. "
                    "Return the complete appended suite with at most 12 cases; labels are not oracle truth."}

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
            actual_models={"challenge": cm, "oracle": om, "review": rm}, control_attempts=attempts)

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
                versions=[version], frozen_item_sha256=digest(c.item.model_dump())
                if p.settings.freeze_question_at_verifier else "")
            c.verifier_assessment = assessment
            self.save(c)
        if assessment.status == "rejected":
            return False
        while True:
            version = assessment.versions[-1]
            index = len(assessment.versions) - 1
            if version.revalidation_pending:
                if p.settings.freeze_question_at_verifier:
                    return self.reject(c, "frozen_item_requires_upstream_new_version")
                if not await revalidate(c):
                    failures = list(c.reasons)
                    p.log("verifier_question_revalidation_failed", candidate=c.id, reasons=failures)
                    return self.reject(c, "question_repair_failed_full_correctness_review:" + ";".join(failures))
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
            if p.settings.freeze_question_at_verifier and (
                    result.review.action == "repair_question" or any(getattr(result.oracle, name).verdict != "pass"
                        for name in ("question_complete", "target_unambiguous", "hidden_steps_recoverable"))):
                return self.reject(c, "upstream_defect_requires_new_version:" + ";".join(errors))
            if index >= p.settings.verifier_max_revisions:
                return self.reject(c, "two_revision_limit_or_configured_limit:" + ";".join(errors))
            action = result.review.action
            question_issue = (result.oracle.question_complete.verdict == "fail"
                              or result.oracle.target_unambiguous.verdict == "fail"
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
