import asyncio
import json

import pytest
from conftest import trials
from pydantic import ValidationError
from test_qc import setup_pipeline

from diamond_factory.config import load
from diamond_factory.difficulty import difficulty_design_errors, parent_solver_feedback, profile_for_category
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    BlueprintAudit,
    DifficultyDesign,
    Question,
    Solution,
    StepwiseBlueprintAudit,
    TaskDesign,
)
from diamond_factory.pipeline import AuthoringRejected


def difficulty_payload():
    return {
        "pattern_ids": ["math_orbit_singularities"],
        "skills": ["orbit closure", "exceptional-set analysis"],
        "skill_bridge": "The closed representation must be known before its exceptional set can be classified.",
        "steps": [
            {"id": "closure", "depends_on": [], "inference": "Recover a closed family from the given transformations."},
            {"id": "singularities", "depends_on": ["closure"], "inference": "Resolve cancellation and all admissible exceptional points."},
            {"id": "target", "depends_on": ["singularities"], "inference": "Extract the invariant from the complete exceptional set."},
        ],
        "bottleneck_step": "singularities",
        "baseline_shortcut": "Read the denominator before checking cancellation and exceptional points.",
        "shortcut_counterexample": "An excluded point may cancel a denominator factor, changing the point count.",
        "easier_control": "Supply the complete exceptional set but still ask for its invariant.",
    }


def task_design(difficulty=None):
    return TaskDesign(source_claim="A synthetic source claim used only to test the contract.",
        claim_locator="fixture section", target="A synthetic exact target", underlying_conditions=["fixture"],
        hidden_steps=["recover representation"], inferable_conditions=[], preserved_invariants=["fixture"],
        inverse_construction="Reconstruct a synthetic observation from the known result.",
        answer_check="Check the synthetic defining equation by exact substitution.", difficulty=difficulty)


@pytest.mark.parametrize("defect", ["cycle", "orphan", "duplicate", "missing_bottleneck"])
def test_claimed_dependency_graph_rejects_disconnected_or_impossible_structure(defect):
    payload = difficulty_payload()
    if defect == "cycle":
        payload["steps"][0]["depends_on"] = ["target"]
    elif defect == "orphan":
        payload["steps"][1]["depends_on"] = []
    elif defect == "duplicate":
        payload["steps"][1]["id"] = "closure"
    else:
        payload["bottleneck_step"] = "absent"
    with pytest.raises(ValidationError):
        DifficultyDesign.model_validate(payload)


def test_connected_claim_is_only_metadata_and_requires_an_applicable_pattern(candidate):
    design = DifficultyDesign.model_validate(difficulty_payload())
    candidate.solution.design = task_design(design)
    profile = {"pattern_cards": [{"id": "math_orbit_singularities", "domain": "Math"}]}
    assert not difficulty_design_errors(candidate.solution, "Math", profile, True)
    assert difficulty_design_errors(candidate.solution, "Physics", profile, True)
    # A valid graph never edits a question, generates a trial or grants acceptance.
    assert candidate.status == "quality_seed" and not candidate.trials


def test_parent_feedback_preserves_real_samples_and_limits_excerpts(candidate):
    candidate.trials = trials(8)
    candidate.trials[0].response = "Begin: " + "x" * 20000 + " Final answer: 36"
    before = candidate.model_dump()
    feedback = parent_solver_feedback(candidate, max_response_chars=200)
    assert feedback["observed_correct"] == 8 and feedback["measurement"] == "complete_16"
    assert [x["untrusted_judge_claim"]["correct"] for x in feedback["examples"]] == ["yes", "no"]
    assert feedback["examples"][0]["response_excerpted"]
    assert len(feedback["examples"][0]["response"]) == 200
    assert feedback["examples"][0]["response"].endswith("Final answer: 36")
    assert candidate.model_dump() == before


@pytest.mark.parametrize("count,correct", [(3, 16), (4, 2)])
def test_partial_or_mixed_prefix_cannot_be_used_as_evolution_evidence(candidate, count, correct):
    candidate.trials = trials(correct)[:count]
    with pytest.raises(ValueError):
        parent_solver_feedback(candidate)


def test_four_correct_stays_a_preliminary_claim_and_profiles_are_category_specific(candidate):
    candidate.trials = trials(16)[:4]
    assert parent_solver_feedback(candidate)["measurement"] == "preliminary_4_of_4"
    profile = {"pattern_cards": [{"id": "m", "domain": "Math"}, {"id": "p", "domain": "Physics"},
                                 {"id": "b", "domain": "Both"}]}
    assert [c["id"] for c in profile_for_category(profile, "Math")["pattern_cards"]] == ["m", "b"]
    assert len(profile["pattern_cards"]) == 3


def test_required_difficulty_design_cannot_bypass_preflight(tmp_path, settings, source, candidate):
    settings.require_solution_design = True
    settings.require_difficulty_design = True
    settings.difficulty_profile_path = "configs/difficulty-archetypes.json"
    candidate.solution.design = task_design()
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    assert not asyncio.run(pipe.preflight(candidate))
    assert "missing_difficulty_design" in candidate.reasons and not gateway.calls


def test_missing_difficulty_design_stops_before_question_generation(tmp_path, settings, source, candidate):
    settings.require_solution_design = True
    settings.require_difficulty_design = True
    settings.difficulty_profile_path = "configs/difficulty-archetypes.json"
    settings.max_repairs = 0
    candidate.solution.design = task_design()
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def call(role, prompt, salt, schema=None, **kwargs):
        gateway.calls.append((role, prompt, salt))
        assert schema is Solution
        return candidate.solution, "fixture", "gpt-6-luna"

    gateway.call = call
    with pytest.raises(AuthoringRejected, match="missing_difficulty_design"):
        asyncio.run(pipe.generate(source, None, {"category": "Math", "subtopic": "组合与图论"}, 0, 0))
    assert len(gateway.calls) == 1


def test_actual_parent_responses_reach_author_prompt(tmp_path, settings, source, candidate):
    settings.early_screen_samples = 4
    candidate.trials = trials(16)[:4]
    candidate.trials[0].response = "Controlled parent solver shortcut, untrusted input only."
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call
    payloads = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        if schema is Solution:
            payloads.append(json.loads(prompt.splitlines()[-1]))
            raise OSError("Stop after capturing author input")
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    with pytest.raises(OSError, match="capturing"):
        asyncio.run(pipe.generate(source, candidate, {"category": "Math", "subtopic": "组合与图论"}, 1, 0))
    assert payloads[0]["parent_solver_feedback"]["examples"][0]["response"] == candidate.trials[0].response
    assert payloads[0]["parent_solver_feedback"]["measurement"] == "preliminary_4_of_4"


def test_new_author_configs_use_six_luna_with_the_same_target_model():
    for name in ("pilot", "production", "math-physics-experiment", "theory100"):
        settings = load("configs/" + name + ".toml")
        assert settings.generator.model == "gpt-6-luna"
        assert "gpt-6-luna-2026-09-22" in settings.generator.accepted_response_models
        assert settings.solver.model == "glm-5.2" and settings.solver.max_tokens is None
        assert settings.request_deadline_seconds == 600


def step_review(count=2, passed=True):
    return StepwiseBlueprintAudit(valid=passed, answer_correct=passed, derivation_complete=passed,
        source_grounded=True, reason="Controlled step-level review used to test the repair protocol.",
        reviewed_step_count=count, issues=[] if passed else [{
            "part": "derivation", "step_index": 2, "claim": "The second synthetic inference is unproved.",
            "evidence": "A controlled unverified step, not an actual mathematical counterexample.",
            "repair_requirement": "Prove the second inference and recheck its downstream answer."}])


@pytest.mark.parametrize("defect", ["no_issues", "no_index", "bad_index"])
def test_rejected_step_review_requires_actionable_correctly_indexed_feedback(defect):
    payload = step_review(passed=False).model_dump()
    if defect == "no_issues":
        payload["issues"] = []
    elif defect == "no_index":
        payload["issues"][0]["step_index"] = None
    else:
        payload["issues"][0]["step_index"] = 3
    with pytest.raises(ValidationError):
        StepwiseBlueprintAudit.model_validate(payload)


def test_two_adversarial_repairs_preserve_full_proof_feedback_before_any_question(tmp_path, settings, source, candidate):
    settings.preverify_blueprint = True
    settings.require_stepwise_blueprint_review = True
    settings.max_repairs = 2
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    author_payloads, reviews, sequence = [], [], []

    async def call(role, prompt, salt, schema=None, **kwargs):
        sequence.append(schema)
        if schema is Solution:
            author_payloads.append(json.loads(prompt.splitlines()[-1]))
            return candidate.solution, "author" + str(len(author_payloads)), "gpt-6-luna"
        if schema is StepwiseBlueprintAudit:
            payload = json.loads(prompt.splitlines()[-1])
            assert "feedback" not in payload and payload["expected_step_count"] == 2
            reviews.append(payload)
            return step_review(passed=len(reviews) == 3), "judge" + str(len(reviews)), "gpt-6-luna"
        assert schema is Question
        raise OSError("Question starts only after the third proof is approved")

    gateway.call = call
    with pytest.raises(OSError, match="third proof"):
        asyncio.run(pipe.generate(source, None, {"category": "Math", "subtopic": "组合与图论"}, 0, 0))
    assert sequence == [Solution, StepwiseBlueprintAudit] * 3 + [Question]
    for payload in author_payloads[1:]:
        feedback = payload["feedback"]
        assert feedback["stage"] == "reference_proof"
        assert feedback["previous"]["derivation"] == candidate.solution.derivation
        assert feedback["blueprint_review"]["issues"][0]["step_index"] == 2


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_invalid_review_step_count_repairs_only_the_judge_not_the_author(tmp_path, settings, source, candidate,
                                                                       repair_succeeds):
    settings.preverify_blueprint = True
    settings.require_stepwise_blueprint_review = True
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    salts = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        assert role == "consistency" and schema is StepwiseBlueprintAudit
        salts.append(salt)
        count = 2 if len(salts) == 2 and repair_succeeds else 3
        return step_review(count=count), "judge", "gpt-6-luna"

    gateway.call = call
    if repair_succeeds:
        review, _, _ = asyncio.run(pipe.review_blueprint(candidate.solution, source.model_dump(), {}, "proof"))
        assert review.reviewed_step_count == 2
    else:
        with pytest.raises(ModelResponseError, match="invalid_blueprint_step_mapping"):
            asyncio.run(pipe.review_blueprint(candidate.solution, source.model_dump(), {}, "proof"))
    assert salts == ["proof", "proof:step-map-repair"]


def test_legacy_boolean_approval_cannot_bypass_required_step_review(tmp_path, settings, source, candidate):
    settings.preverify_blueprint = True
    settings.require_stepwise_blueprint_review = True
    candidate.blueprint_audit = BlueprintAudit(valid=True, answer_correct=True, derivation_complete=True,
        source_grounded=True, reason="An older approval without an indexed proof review.")
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    assert not asyncio.run(pipe.preflight(candidate))
    assert "missing_or_mismatched_stepwise_blueprint_review" in candidate.reasons and not gateway.calls
