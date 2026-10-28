"""科学目标与答案形式分离；合成对照只验证合同，不作为难度成绩。"""
import asyncio
import json

import pytest
from pydantic import ValidationError
from test_bottleneck import prepare, stamp
from test_qc import setup_pipeline

from diamond_factory.config import load
from diamond_factory.difficulty import construction_ready
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    ResultSelection,
    ScientificGoal,
    ScientificGoalReferences,
    ScientificGoalSelection,
)
from diamond_factory.pipeline import AuthoringRejected, QualityRejected, SampleIncomplete


def author(tmp_path, source, candidate):
    settings = load("configs/library-paper-hard-step.toml")
    prepare(candidate, source)
    candidate.correctness_basis = "paper_grounded"
    candidate.source_fidelity = "advisory"
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    goal = ScientificGoal(source_id=source.id, target=candidate.known_result.target,
        assumptions=candidate.known_result.assumptions,
        reasoning_challenge=["Synthetic goal: establish the graph property from primitive assumptions."],
        reference_case_ids=[pipe.goal_references.cases[0].id],
        reference_comparison="Only the construction-versus-global-completeness structure is analogous.")
    return pipe, store, gateway, goal


def select(pipe, source):
    return asyncio.run(pipe.select_known_result(source, source.model_dump(),
        {"category": "Math", "subtopic": "组合与图论"}, "line", None))


def replies(gateway, goal, result, *, wrong_first_target=False):
    calls = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        calls.append((schema, salt, json.loads(prompt.splitlines()[-1])))
        if schema is ScientificGoalSelection:
            return ScientificGoalSelection(outcome="found", goal=goal, reason="Synthetic goal contract fixture."), salt, "m"
        if schema is ResultSelection:
            value = result.model_copy(deep=True)
            value.grading_requirement = result.grading_requirement
            if wrong_first_target and salt.endswith(":0"):
                value.target = "A different target selected to make the answer shorter."
            return ResultSelection(outcome="found", result=value, reason="Synthetic answer contract fixture."), salt, "m"
        raise AssertionError(schema)

    gateway.call = call
    return calls


@pytest.mark.parametrize("answer", ["The admissible objects form exactly the stated equivalence class.",
                                   "yes", "n-1", "A qualitative classification with cases. " * 350])
def test_goal_precedes_answer_and_does_not_filter_by_form(tmp_path, source, candidate, answer):
    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    result = candidate.known_result.model_copy(update={"answer": answer})
    calls = replies(gateway, goal, result)
    chosen = select(pipe, source)
    assert [c[0] for c in calls] == [ScientificGoalSelection, ResultSelection]
    assert "answer" not in calls[0][2] and "answer" not in ScientificGoal.model_fields
    assert calls[1][2]["scientific_goal"] == goal.model_dump()
    assert chosen.answer == answer and chosen.scientific_goal.goal == goal
    assert chosen.scientific_goal.request_id == "line:scientific-goal:0"
    assert chosen.scientific_goal.actual_model == "m"


def test_answer_selection_cannot_trade_away_the_scientific_goal(tmp_path, source, candidate):
    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    calls = replies(gateway, goal, candidate.known_result, wrong_first_target=True)
    result = select(pipe, source)
    assert [c[0] for c in calls].count(ScientificGoalSelection) == 1
    assert len(calls) == 3 and result.target == goal.target
    assert "selected_scientific_goal_changed" in calls[-1][2]["feedback"]["errors"]


def test_unknown_reference_stops_before_answer_selection(tmp_path, source, candidate):
    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    goal.reference_case_ids = ["invented-measured-0-of-16"]
    calls = replies(gateway, goal, candidate.known_result)
    with pytest.raises(AuthoringRejected, match="invalid_scientific_goal_reference_ids"):
        select(pipe, source)
    assert all(c[0] is ScientificGoalSelection for c in calls)


def test_goal_model_failure_is_not_difficulty_or_answer_failure(tmp_path, source, candidate):
    pipe, _store, gateway, _goal = author(tmp_path, source, candidate)

    async def fail(*args, **kwargs):
        raise ModelResponseError("generator", "failed-goal", "invalid_schema")

    gateway.call = fail
    with pytest.raises(ModelResponseError, match="invalid_schema"):
        select(pipe, source)
    assert not candidate.trials


@pytest.mark.parametrize("change", ["missing", "content", "source", "reference", "prompt", "model", "target", "assumption"])
def test_goal_approval_changes_block_sampling_and_export(tmp_path, source, candidate, monkeypatch, change):
    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    replies(gateway, goal, candidate.known_result)
    candidate.known_result = select(pipe, source)
    stamp(pipe, candidate, source)
    monkeypatch.setattr("diamond_factory.verifier.has_valid_verifier", lambda *a, **k: True)
    monkeypatch.setattr("diamond_factory.difficulty.valid_bottleneck", lambda *a, **k: True)
    assert construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)
    record = candidate.known_result.scientific_goal
    if change == "missing":
        candidate.known_result.scientific_goal = None
    elif change == "content":
        record.goal.reasoning_challenge = ["Changed after selecting the scientific objective"]
    elif change == "source":
        record.source_sha256 = "changed"
    elif change == "reference":
        record.references_sha256 = "changed"
    elif change == "prompt":
        record.prompt_sha256 = "changed"
    elif change == "model":
        record.model_config_sha256 = "changed"
    elif change == "target":
        candidate.known_result.target = "A different goal chosen after seeing the answer."
    else:
        candidate.known_result.assumptions = ["A different primitive premise"]
    assert not construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)
    candidate.authoring_complete = True
    # 通用采样也必须走实际目标身份核验，不能只相信曾通过的蓝图。
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(candidate, 4))
    assert any("scientific_goal" in e for e in candidate.reasons)


def test_proof_goal_is_selectable_but_not_falsely_graded_by_hle(tmp_path, source, candidate):
    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    result = candidate.known_result.model_copy(update={"grading_requirement": "argument_or_witness_validation"})
    calls = replies(gateway, goal, result)
    candidate.known_result = select(pipe, source)
    assert len(calls) == 2
    candidate.authoring_complete = True
    with pytest.raises(SampleIncomplete, match="evaluation_contract_uncovered"):
        asyncio.run(pipe.sample(candidate, 4))
    assert len(calls) == 2 and not candidate.trials
    assert not construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)


def test_independent_proof_classification_cannot_be_overridden_by_author(tmp_path, source, candidate):
    from conftest import passing_consistency
    from test_paper_hard_step import assessment

    pipe, _store, gateway, goal = author(tmp_path, source, candidate)
    calls = replies(gateway, goal, candidate.known_result)
    candidate.known_result = select(pipe, source)
    review = passing_consistency()
    review.task_kind = "proof"
    candidate.consistency = assessment(review)
    candidate.authoring_complete = True
    with pytest.raises(SampleIncomplete, match="evaluation_contract_uncovered"):
        asyncio.run(pipe.sample(candidate, 4))
    assert len(calls) == 2 and not candidate.trials
    assert not construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)


def test_references_are_not_mislabeled_as_measured_and_change_run_fingerprint(tmp_path, source, candidate):
    pipe, _store, _gateway, _goal = author(tmp_path, source, candidate)
    references = pipe.goal_references
    assert all(c.evidence_status == "public_solution_not_glm_calibrated" for c in references.cases)
    changed = references.model_copy(deep=True)
    changed.cases[0].mechanism.append("Changed design reference")
    path = tmp_path / "changed-references.json"
    path.write_text(changed.model_dump_json())
    second, _, _ = setup_pipeline(tmp_path / "second",
        pipe.settings.model_copy(update={"scientific_goal_references_path": str(path)}), source, candidate)
    assert second.fingerprint != pipe.fingerprint
    with pytest.raises(ValidationError):
        ScientificGoalReferences.model_validate({**references.model_dump(),
            "cases": [references.cases[0].model_dump(), references.cases[0].model_dump()]})
