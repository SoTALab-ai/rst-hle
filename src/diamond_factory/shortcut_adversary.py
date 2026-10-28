"""只看公开输入的短解攻击、历史路线重查与后置难度诊断。"""
import json

from .llm import ModelResponseError
from .models import DifficultyChallenge, DifficultyDiagnostic, ShortcutAttack, ShortcutAttackRecord, digest


def public_inputs(version):
    if not version.proposal.public_task:
        raise ValueError("Shortcut attacks require the exact public task, not a retained-input summary")
    return {"stage": "before_lock", "question": version.proposal.public_task}


def public_question(item):
    return {"stage": "actual_question", "question": item.question, "required_outputs": item.required_outputs}


def proposal_routes(versions):
    return [{"route_id": v.proposal_id, "previous_task": public_inputs(v),
             "route": v.challenge.shortest_correct_route}
            for v in versions if v.challenge and v.challenge.verdict == "too_easy"]


def question_routes(rounds):
    return [{"route_id": r.input_sha256, "previous_task": public_question(r.item),
             "route": r.challenge.shortest_correct_route}
            for r in rounds if r.challenge.verdict == "too_easy"]


def record_valid(record, packet):
    return bool(record and record.request_id and record.actual_model and record.input_sha256 == digest(packet))


def review_errors(review, routes):
    expected = {r["route_id"] for r in routes}
    actual = [r.route_id for r in review.route_rechecks]
    errors = []
    if set(actual) != expected or len(actual) != len(expected):
        errors.append("shortcut_route_coverage_incomplete")
    if review.attack_validity is None:
        errors.append("shortcut_attack_not_verified")
    if review.verdict == "sufficient" and any(r.verdict in ("still_bypass", "uncertain")
                                             for r in review.route_rechecks):
        errors.append("shortcut_prior_route_unresolved")
    return errors


async def attack(p, packet, salt):
    # 此调用绝不包含论文、golden、作者推导、预期桥梁或旧攻击反馈。
    value, rid, model = await p.consistency_call(p.prompts["shortcut_attack"] + "\n" +
        json.dumps(packet, ensure_ascii=False), salt, ShortcutAttack)
    return ShortcutAttackRecord(input_sha256=digest(packet), response=value, request_id=rid, actual_model=model)


async def diagnose(p, candidate, input_hash):
    current = candidate.difficulty_diagnostic
    if current and current.input_sha256 == input_hash:
        return current
    value, rid, model = await p.consistency_call(p.prompts["shortcut_depth_diagnostic"] + "\n" + json.dumps({
        "stage": "actual_question", "question": candidate.item.question,
        "required_outputs": candidate.item.required_outputs, "solution": candidate.solution.model_dump(),
        "locked_result": candidate.known_result.model_dump(),
        "independent_solution": candidate.consistency.independent_solution.model_dump()}, ensure_ascii=False),
        f"{candidate.id}:post-shortcut-depth:{input_hash}", DifficultyChallenge)
    if max(value.indispensable_proof_entries, default=0) > len(candidate.solution.derivation):
        raise ModelResponseError("consistency", rid, "invalid_difficulty_proof_mapping")
    result = DifficultyDiagnostic(input_sha256=input_hash, assessment=value, request_id=rid, actual_model=model)
    candidate.difficulty_diagnostic = result
    p.store.save(candidate)
    p.log("post_shortcut_depth_diagnostic", candidate=candidate.id, verdict=value.verdict,
          advisory=True, request_id=rid, actual_model=model)
    return result
