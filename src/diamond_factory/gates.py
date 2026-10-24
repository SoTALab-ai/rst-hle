import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from .models import Candidate, DeadlineOutcome, Record, Source, digest


def normalize(text: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", text).casefold())


def lexical_duplicate(a: str, b: str) -> bool:
    if normalize(a) == normalize(b):
        return True
    # Character shingles also cover Chinese; semantic dedup separately detects changed numbers/material.
    a, b = normalize(a), normalize(b)
    x = {a[i:i+5] for i in range(max(0, len(a)-4))}
    y = {b[i:i+5] for i in range(max(0, len(b)-4))}
    return bool(x and y) and len(x & y) / len(x | y) >= 0.72


def evidence_errors(evidence, sources: dict[str, Source]) -> list[str]:
    errors = []
    for ev in evidence:
        source = sources.get(ev.source_id)
        if not source or " ".join(ev.quote.split()) not in " ".join(source.text.split()):
            errors.append("evidence_quote_not_in_source")
    return errors


def static_errors(candidate: Candidate, sources: dict, quotas: dict) -> list[str]:
    q = candidate.item
    errors = evidence_errors(q.evidence + candidate.solution.evidence, sources)
    def has_control(value):
        if isinstance(value,str):
            return bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]|\r(?!\n)",value))
        if isinstance(value,dict):
            return any(has_control(v) for v in value.values())
        if isinstance(value,list):
            return any(has_control(v) for v in value)
        return False
    if has_control(q.model_dump()) or has_control(candidate.solution.model_dump()):
        errors.append("invalid_control_character_in_math_text")
    if not any(x["category"] == q.category and x["subtopic"] == q.subtopic for x in quotas["quotas"]):
        errors.append("unknown_v3_category_subtopic")
    if q.answer != candidate.solution.answer:
        errors.append("solution_question_answer_mismatch")
    if re.search(r"!\[|<img|\bsee (?:the )?(?:image|figure|diagram)\b", q.question, re.IGNORECASE):
        errors.append("potential_image_dependency")
    if q.answer_type == "multipleChoice":
        if not re.search(r"(?m)^A[.)]\s", q.question) or not re.search(r"(?m)^E[.)]\s", q.question):
            errors.append("missing_multiple_choice_material")
        if not re.fullmatch(r"[A-Z]", q.answer.strip()):
            errors.append("invalid_choice_answer")
        elif not re.search(r"(?m)^" + q.answer.strip() + r"[.)]\s", q.question):
            errors.append("answer_choice_not_present")
    return sorted(set(errors))


def sample_outcomes(candidate):
    return list(candidate.trials) + list(candidate.deadline_outcomes)


def correct_count(trials, samples=16) -> int:
    if len(trials) != samples or {x.sample_id for x in trials} != set(range(samples)):
        raise ValueError("Incomplete or duplicate samples; an answer or verified solver deadline is required")
    if len({x.solver_request for x in trials}) != samples:
        raise ValueError("Solver samples were reused")
    answered = [x for x in trials if not isinstance(x, DeadlineOutcome)]
    if len({x.judge_request for x in answered}) != len(answered):
        raise ValueError("Judge samples were reused")
    return sum(x.judgment.correct == "yes" for x in answered)


def read_corpus(path: str):
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            rows.append(Record.model_validate_json(line))
    if not rows:
        raise ValueError(f"Empty dedup corpus: {path}")
    return rows


def production_blockers(settings, quotas: dict) -> list[str]:
    errors = []
    if (settings.samples, settings.correct_min, settings.correct_max) != (16, 1, 8):
        errors.append("requires_16_samples_and_1_to_8_correct")
    if settings.solver.model != "glm-5.2" or settings.judge.model != "gpt-6-luna":
        errors.append("procurement_model_config_mismatch")
    if settings.judge.reasoning_effort != "medium":
        errors.append("judge_requires_medium")
    if len(quotas["quotas"]) != 61 or sum(x["count"] for x in quotas["quotas"]) != 2000:
        errors.append("procurement_quota_manifest_mismatch")
    if settings.external_dedup_required and len(settings.dedup_corpora) < 2:
        errors.append("train_and_eval_dedup_corpora_required")
    else:
        for path in settings.dedup_corpora:
            try:
                read_corpus(path)
            except (ValueError, OSError):
                errors.append("invalid_or_missing_dedup_corpus:" + path)
    contract = json.loads(Path(settings.judge_contract).read_text())
    prompt = Path(settings.prompts_dir, "judge.txt").read_text()
    if not contract.get("approved") or contract.get("prompt_sha256") != digest(prompt):
        errors.append("official_hle_judge_contract_mismatch")
    system = Path(settings.prompts_dir, "judge_system.txt")
    if ((system.exists() and contract.get("system_prompt_sha256") != digest(system.read_text()))
            or (not system.exists() and contract.get("system_prompt_sha256"))):
        errors.append("judge_system_contract_mismatch")
    for key in ("evaluation_code_revision", "extraction_rule", "semantic_equivalence_rule", "numeric_tolerance_rule"):
        if not contract.get(key):
            errors.append("judge_contract_missing:" + key)
    return errors


def can_count(candidate, prior, quotas, max_per_root=1) -> list[str]:
    accepted = [c for c in prior if c.status == "accepted" and c.id != candidate.id]
    errors = []
    key = (candidate.item.category, candidate.item.subtopic)
    quota = next((x["count"] for x in quotas["quotas"]
                  if (x["category"], x["subtopic"]) == key), 0)
    counts = Counter((c.item.category, c.item.subtopic) for c in accepted)
    if counts[key] >= quota:
        errors.append("subtopic_quota_full")
    if sum(c.root_id == candidate.root_id for c in accepted) >= max_per_root:
        errors.append("lineage_not_independent")
    if any(c.item.material_key == candidate.item.material_key for c in accepted):
        errors.append("material_already_counted")
    return errors
