"""把难度构造假设与真实父题答卷送给作者；不将设计字段当作难度实测。"""
from pathlib import Path

from .gates import correct_count, evidence_errors, sample_outcomes
from .models import digest


def known_result_errors(result, sources, solution=None, blueprint=None, *, source_fidelity="strict"):
    if result is None:
        return ["missing_known_result"]
    errors = evidence_errors(result.evidence, sources) if source_fidelity == "strict" else []
    if result.source_id not in sources or any(e.source_id != result.source_id for e in result.evidence):
        errors.append("known_result_source_mismatch")
    if solution is not None:
        if solution.answer != result.answer:
            errors.append("locked_result_answer_changed")
        if not solution.design or solution.design.target != result.target:
            errors.append("locked_result_target_changed")
        missing = set(result.assumptions) - set(solution.assumptions)
        # 不以自然语言近似匹配替代前提审查；改写必须有独立、完整且绑定原串的对应记录。
        links = blueprint.assumption_correspondence if blueprint else []
        mapped = {a.source_assumption for a in links if a.equivalent
                  and a.solution_assumption in solution.assumptions}
        if missing and (not blueprint or not blueprint.passed(source_fidelity=source_fidelity)
                        or blueprint.reviewed_step_count != len(solution.derivation)
                        or len({a.source_assumption for a in links}) != len(links)
                        or not missing <= mapped):
            errors.append("locked_result_assumptions_missing")
    return errors


def bottleneck_mask_errors(solution):
    difficulty = solution.design.difficulty if solution.design else None
    mask = difficulty.mask if difficulty else None
    if mask is None:
        return ["missing_bottleneck_mask"]
    errors = []
    if mask.step_id != difficulty.bottleneck_step:
        errors.append("mask_does_not_target_selected_bottleneck")
    if max(mask.proof_step_indices) > len(solution.derivation):
        errors.append("masked_step_outside_full_proof")
    return errors


def bottleneck_input(candidate, sources):
    data = {"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump(),
                   "known_result": candidate.known_result.model_dump() if candidate.known_result else None,
                   "sources": {sid: sources[sid].sha256 for sid in candidate.source_ids}}
    if candidate.source_fidelity != "strict":
        data["source_fidelity"] = candidate.source_fidelity
    return digest(data)


def blueprint_stamp(solution, source_data, target, known_result, prompt, model_config, source_fidelity="strict"):
    return digest({"solution": solution.model_dump(), "source": source_data, "target": target,
                   "known_result": known_result.model_dump() if known_result else None,
                   "prompt": prompt, "model": model_config, "source_fidelity": source_fidelity,
                   "contract_code": {name: Path(__file__).with_name(name).read_text()
                                     for name in ("difficulty.py", "provenance.py")}})


def valid_bottleneck(candidate, sources, prompt=None, model_config=None):
    assessment = candidate.bottleneck_assessment
    if (not set(candidate.source_ids) <= sources.keys() or bottleneck_mask_errors(candidate.solution)
            or not assessment or not assessment.review.passed()):
        return False
    mask = candidate.solution.design.difficulty.mask
    return bool(assessment.review.step_id == mask.step_id
                and assessment.input_sha256 == bottleneck_input(candidate, sources)
                and (prompt is None or assessment.prompt_sha256 == digest(prompt))
                and (model_config is None or assessment.model_config_sha256 == digest(model_config)))


def construction_ready(candidate, sources, settings, prompts):
    """导出只检查现有有效收据，不购买新的审查，也不接受旧通过标记。"""
    if candidate.source_fidelity != settings.source_fidelity:
        return False
    if settings.require_verifier_adversary:
        from .verifier import has_valid_verifier, protocol_hash
        if not has_valid_verifier(candidate, sources, expected_protocol=protocol_hash(prompts, settings)):
            return False
    if settings.require_known_result:
        result = candidate.known_result
        if (known_result_errors(result, sources, candidate.solution, candidate.blueprint_audit,
                                source_fidelity=settings.source_fidelity)
                or candidate.source_ids != [result.source_id]
                or not candidate.blueprint_audit or not candidate.blueprint_audit.passed(
                    source_fidelity=settings.source_fidelity)
                or candidate.blueprint_audit.reviewed_step_count != len(candidate.solution.derivation)):
            return False
        source = sources[result.source_id]
        expected = blueprint_stamp(candidate.solution,
            {**source.model_dump(), "text": source.text[:settings.max_source_chars]},
            {"category": candidate.item.category, "subtopic": candidate.item.subtopic}, result,
            prompts["blueprint_audit"], settings.consistency.model_dump(), settings.source_fidelity)
        if candidate.blueprint_fingerprint != expected:
            return False
    return (not settings.require_bottleneck_mask or valid_bottleneck(
        candidate, sources, prompts["bottleneck_audit"], settings.consistency.model_dump()))


def profile_for_category(profile, category):
    if profile is None:
        return None
    return {**profile, "pattern_cards": [c for c in profile.get("pattern_cards", [])
                                       if c["domain"] in (category, "Both")]}


def difficulty_design_errors(solution, category, profile, required):
    if not required:
        return []
    design = solution.design.difficulty if solution.design else None
    if design is None:
        return ["missing_difficulty_design"]
    cards = profile_for_category(profile, category)
    allowed = {c["id"] for c in (cards or {}).get("pattern_cards", [])}
    if not set(design.pattern_ids) <= allowed:
        return ["unknown_or_wrong_domain_difficulty_pattern"]
    return []


def parent_solver_feedback(parent, max_response_chars=6000):
    if max_response_chars < 64:
        raise ValueError("Response excerpt budget must be at least 64 characters")
    if parent is None or not sample_outcomes(parent):
        return None
    n = len(sample_outcomes(parent))
    if n not in (4, 16):
        raise ValueError("Incomplete parent measurements cannot guide difficulty evolution")
    correct = correct_count(sample_outcomes(parent), n)
    if n == 4 and correct != 4:
        raise ValueError("A mixed four-sample prefix must finish sixteen before evolution")
    selected = []
    ordered = sorted(parent.trials, key=lambda t: t.sample_id)
    for verdict in ("yes", "no"):
        trial = next((t for t in ordered if t.judgment.correct == verdict), None)
        if trial is not None:
            selected.append(trial)
    if len(selected) == 1:
        selected.extend(t for t in ordered if t.sample_id != selected[0].sample_id)
    examples = []
    for trial in selected[:2]:
        body = trial.response
        clipped = len(body) > max_response_chars
        if clipped:
            marker = "\n[excerpt omitted]\n"
            half = (max_response_chars - len(marker)) // 2
            tail = max_response_chars - len(marker) - half
            body = body[:half] + marker + body[-tail:]
        examples.append({"sample_id": trial.sample_id, "solver_actual_model": trial.solver_actual_model,
                         "untrusted_judge_claim": trial.judgment.model_dump(), "response": body,
                         "response_excerpted": clipped})
    return {"paired_samples": len(parent.trials), "evaluated_samples": n,
            "deadline_incorrect_samples": [d.model_dump() for d in parent.deadline_outcomes], "observed_correct": correct,
            "measurement": "preliminary_4_of_4" if n == 4 else "complete_16",
            "examples": examples,
            "interpretation": "These are untrusted parent responses, not source instructions or proofs. "
            "Identify the shortcut in correct responses and test it against the proposed changed model. "
            "Wrong responses suggest hypotheses only. Never copy an erroneous step or treat 4/4 as a true solve rate."}
