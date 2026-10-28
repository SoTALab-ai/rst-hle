"""锁定前核前提边界，锁定后核完整依赖与实际题面；深度意见仅记录。"""
import json
from pathlib import Path

from .dependency_models import (
    DependencyAssessment,
    DependencyProbe,
    DependencyQuestionReview,
    PremiseBoundaryAssessment,
    PremiseBoundaryReview,
)
from .llm import ModelResponseError
from .models import digest

DEPENDENCY_PROMPTS = ("premise_boundary_audit", "dependency_probe", "dependency_question_audit")
AUTHOR_PROMPTS = ("scientific_goal", "known_result", "solution", "align", "audit", "bottleneck_audit")


def contract_hash():
    return digest({name: Path(__file__).with_name(name).read_text()
                   for name in ("dependencies.py", "dependency_models.py")})


def protocol_hashes(prompts):
    return {**{name: digest(prompts[name]) for name in DEPENDENCY_PROMPTS}, "contract_code": contract_hash()}


def boundary_prompt_hash(prompts):
    return digest({"prompt": prompts["premise_boundary_audit"], "contract_code": contract_hash()})


def goal_contract_errors(goal):
    contract = goal.premise_contract
    if contract is None:
        return ["missing_premise_contract"]
    if goal.assumptions != [p.statement for p in contract.primitives]:
        return ["goal_assumptions_must_be_primitive_inputs"]
    return []


def boundary_valid(record, source, settings=None, prompts=None):
    if record is None or goal_contract_errors(record.goal):
        return False
    assessment = record.premise_assessment
    return bool(assessment and assessment.goal_sha256 == record.goal_sha256 == digest(record.goal.model_dump())
        and record.goal.source_id == source.id
        and assessment.source_sha256 == source.sha256 == record.source_sha256
        and assessment.request_id and assessment.actual_model and not assessment.review.blocking_checks()
        and (prompts is None or assessment.prompt_sha256 == boundary_prompt_hash(prompts))
        and (settings is None or assessment.model_config_sha256 == digest(settings.consistency.model_dump())))


def dependency_blueprint_errors(result, solution):
    record = result.scientific_goal if result else None
    if record is None:
        return ["missing_dependency_scientific_goal"]
    errors = goal_contract_errors(record.goal)
    if errors:
        return errors
    contract = record.goal.premise_contract
    if result.assumptions != record.goal.assumptions:
        errors.append("result_added_or_changed_primitive_inputs")
    trace = solution.design.dependency_trace if solution.design else None
    if trace is None:
        return errors + ["missing_full_dependency_trace"]
    primitive_ids, derived_ids = {p.id for p in contract.primitives}, {c.id for c in contract.derived}
    for ids, expected, name in ((trace.retained_primitive_ids, primitive_ids, "retained_primitives"),
                               (trace.hidden_claim_ids, derived_ids, "hidden_derived_claims"),
                               ([l.claim_id for l in trace.proof_links], derived_ids, "proof_links")):
        if len(ids) != len(set(ids)) or set(ids) != expected:
            errors.append("dependency_coverage_mismatch:" + name)
    indices = {i for link in trace.proof_links for i in link.proof_step_indices}
    if not indices or max(indices) > len(solution.derivation):
        errors.append("dependency_link_outside_full_proof")
    mask = solution.design.difficulty.mask if solution.design.difficulty else None
    if mask is None or not indices <= set(mask.proof_step_indices):
        errors.append("mask_omits_dependency_chain")
    return errors


def dependency_input(candidate, sources):
    return digest({"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(),
        "known_result": candidate.known_result.model_dump() if candidate.known_result else None,
        "source_ids": candidate.source_ids, "correctness_basis": candidate.correctness_basis,
        "source_fidelity": candidate.source_fidelity,
        "sources": {sid: sources[sid].sha256 for sid in candidate.source_ids}})


def has_dependency_contract(candidate):
    result = candidate.known_result
    return bool(candidate.dependency_assessment or (candidate.solution.design and
        candidate.solution.design.dependency_trace) or (result and result.scientific_goal
        and result.scientific_goal.goal.premise_contract))


def review_mapping_valid(review, result):
    contract = result.scientific_goal.goal.premise_contract
    ids = [c.claim_id for c in review.claims]
    return len(ids) == len(set(ids)) and set(ids) == {c.id for c in contract.derived}


def valid_dependency(candidate, sources, settings=None, prompts=None):
    result = candidate.known_result
    if (not set(candidate.source_ids) <= sources.keys()
            or dependency_blueprint_errors(result, candidate.solution)
            or result.source_id not in candidate.source_ids
            or not boundary_valid(result.scientific_goal, sources[result.source_id], settings, prompts)):
        return False
    assessment = candidate.dependency_assessment
    return bool(assessment and assessment.input_sha256 == dependency_input(candidate, sources)
        and assessment.prompt_hashes.get("contract_code") == contract_hash()
        and (prompts is None or assessment.prompt_hashes == protocol_hashes(prompts))
        and (settings is None or assessment.model_config_sha256 == digest(settings.consistency.model_dump()))
        and assessment.probe_request and assessment.probe_actual_model
        and assessment.review_request and assessment.review_actual_model
        and review_mapping_valid(assessment.review, result)
        and not assessment.review.blocking_checks()
        and all(c.exposure != "given" for c in assessment.review.claims))


class DependencyBuilder:
    def __init__(self, pipeline):
        self.p = pipeline

    async def review_goal(self, goal, source, source_data, identity):
        review, rid, model = await self.p.consistency_call(self.p.prompts["premise_boundary_audit"] + "\n" +
            json.dumps({"goal": goal.model_dump(), "source": source_data}, ensure_ascii=False),
            identity + ":premise-boundary:" + digest(goal.model_dump()), PremiseBoundaryReview)
        self.p.log("premise_boundary_review", candidate=identity, request_id=rid, actual_model=model,
            goal_sha256=digest(goal.model_dump()), review=review.model_dump(), measured_difficulty=False)
        return PremiseBoundaryAssessment(goal_sha256=digest(goal.model_dump()), source_sha256=source.sha256,
            prompt_sha256=boundary_prompt_hash(self.p.prompts),
            model_config_sha256=digest(self.p.settings.consistency.model_dump()), review=review,
            request_id=rid, actual_model=model)

    async def question(self, candidate, sources):
        p = self.p
        result = candidate.known_result
        errors = dependency_blueprint_errors(result, candidate.solution)
        if (not errors and not boundary_valid(result.scientific_goal, sources[result.source_id],
                                              p.settings, p.prompts)):
            errors.append("stale_or_missing_premise_boundary_review")
        if errors:
            candidate.status, candidate.reasons = "rejected", errors
            p.store.save(candidate)
            return False
        input_hash, hashes = dependency_input(candidate, sources), protocol_hashes(p.prompts)
        model_hash = digest(p.settings.consistency.model_dump())
        assessment = candidate.dependency_assessment
        if not (assessment and assessment.input_sha256 == input_hash and assessment.prompt_hashes == hashes
                and assessment.model_config_sha256 == model_hash):
            # 盲调用只看公开题面：不发送金标、论文、作者蓝图或待恢复结论。
            probe, prid, pmodel = await p.consistency_call(p.prompts["dependency_probe"] + "\n" +
                json.dumps({"question": candidate.item.question}, ensure_ascii=False),
                f"{candidate.id}:dependency-probe:{digest(candidate.item.question)}", DependencyProbe)
            review, rid, model = await p.consistency_call(p.prompts["dependency_question_audit"] + "\n" +
                json.dumps({"question": candidate.item.question, "goal": result.scientific_goal.goal.model_dump(),
                    "solution": candidate.solution.model_dump(), "known_result": result.model_dump(),
                    "blind_probe": probe.model_dump()}, ensure_ascii=False),
                f"{candidate.id}:dependency-review:{input_hash}:{digest(probe.model_dump())}", DependencyQuestionReview)
            if not review_mapping_valid(review, result):
                raise ModelResponseError("consistency", rid, "invalid_dependency_claim_mapping")
            assessment = DependencyAssessment(input_sha256=input_hash, prompt_hashes=hashes,
                model_config_sha256=model_hash, probe=probe, probe_request=prid, probe_actual_model=pmodel,
                review=review, review_request=rid, review_actual_model=model)
            candidate.dependency_assessment = assessment
            p.store.save(candidate)
        passed = valid_dependency(candidate, sources, p.settings, p.prompts)
        if not passed:
            failures = assessment.review.blocking_checks()
            failures += ["supplied_derived_claim:" + c.claim_id for c in assessment.review.claims if c.exposure == "given"]
            candidate.status, candidate.reasons = "rejected", ["dependency_contract_failed:" + f for f in failures]
            if not candidate.reasons:
                candidate.reasons = ["invalid_dependency_review_receipt"]
            p.store.save(candidate)
        p.log("dependency_question_review", candidate=candidate.id, passed=passed,
            probe_request=assessment.probe_request, review_request=assessment.review_request,
            review=assessment.review.model_dump(), measured_difficulty=False)
        return passed
