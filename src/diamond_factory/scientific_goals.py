"""科学目标优先：核对目标、来源及参考案例身份，不把案例标签当成难度成绩。"""
from pathlib import Path

from .models import ScientificGoalReferences, digest


def load_references(settings):
    path = settings.scientific_goal_references_path
    return ScientificGoalReferences.model_validate_json(Path(path).read_text()) if path else None


def goal_errors(result, sources, references, prompt, model_config):
    if references is None:
        return []
    record = result.scientific_goal if result else None
    if record is None:
        return ["missing_scientific_goal_selection"]
    goal = record.goal
    errors = []
    if record.goal_sha256 != digest(goal.model_dump()):
        errors.append("scientific_goal_content_changed")
    if (goal.source_id != result.source_id or goal.source_id not in sources
            or record.source_sha256 != sources[goal.source_id].sha256):
        errors.append("scientific_goal_source_changed")
    if record.references_sha256 != digest(references.model_dump()):
        errors.append("scientific_goal_references_changed")
    if record.prompt_sha256 != digest(prompt) or record.model_config_sha256 != digest(model_config):
        errors.append("scientific_goal_protocol_changed")
    if (len(set(goal.reference_case_ids)) != len(goal.reference_case_ids)
            or not set(goal.reference_case_ids) <= {c.id for c in references.cases}):
        errors.append("invalid_scientific_goal_reference_ids")
    if result.target != goal.target or not set(goal.assumptions) <= set(result.assumptions):
        errors.append("selected_scientific_goal_changed")
    if not record.request_id or not record.actual_model:
        errors.append("missing_scientific_goal_receipt")
    return errors
