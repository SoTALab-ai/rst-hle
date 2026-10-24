"""把难度构造假设与真实父题答卷送给作者；不将设计字段当作难度实测。"""
from .gates import correct_count


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
    if parent is None or not parent.trials:
        return None
    n = len(parent.trials)
    if n not in (4, 16):
        raise ValueError("Incomplete parent measurements cannot guide difficulty evolution")
    correct = correct_count(parent.trials, n)
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
    return {"paired_samples": n, "observed_correct": correct,
            "measurement": "preliminary_4_of_4" if n == 4 else "complete_16",
            "examples": examples,
            "interpretation": "These are untrusted parent responses, not source instructions or proofs. "
            "Identify the shortcut in correct responses and test it against the proposed changed model. "
            "Wrong responses suggest hypotheses only. Never copy an erroneous step or treat 4/4 as a true solve rate."}
