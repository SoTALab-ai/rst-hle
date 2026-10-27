"""检查实际阶段请求与决策的一致性；合成案例不提供科学或难度成绩。"""
import asyncio
import json

import pytest
from test_authoring_yield import draft_for
from test_bottleneck import enable, mask_review, prepare
from test_difficulty import step_review
from test_experiment import pricing
from test_qc import setup_pipeline

from diamond_factory.experiment import ExperimentPlan, QuestionExperiment, RootPlan
from diamond_factory.models import (
    Audit,
    BlueprintIssue,
    BottleneckReview,
    CertificateAudit,
    LockedProofDraft,
    Question,
    ResultSelection,
    StepwiseBlueprintAudit,
)
from diamond_factory.pipeline import AuthoringRejected
from diamond_factory.verifier import VerifierBuilder


@pytest.mark.parametrize("basis", ["self_verified", "paper_grounded"])
def test_mixed_proof_and_depth_feedback_repairs_only_under_deferred_policy(
        tmp_path, settings, source, candidate, basis):
    enable(settings)
    settings.correctness_basis = basis
    settings.use_locked_proof_draft = True
    settings.max_repairs = 1
    prepare(candidate, source)
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call
    requests = []
    proof_reviews = 0

    async def call(role, prompt, salt, schema=None, **kwargs):
        nonlocal proof_reviews
        data = json.loads(prompt.splitlines()[-1])
        requests.append((schema, data))
        if schema is ResultSelection:
            return ResultSelection(outcome="found", result=candidate.known_result,
                reason="Synthetic locked target for a repair-path control."), salt, "fixture"
        if schema is LockedProofDraft:
            if proof_reviews:
                instruction = data["feedback"]["repair_instruction"]
                assert "do not emit gold" in instruction and "corrected proof and gold" not in instruction
            return draft_for(candidate.solution), salt, "fixture"
        if schema is StepwiseBlueprintAudit:
            proof_reviews += 1
            review = step_review()
            if proof_reviews == 1:
                review.valid = review.derivation_complete = False
                review.issues = [BlueprintIssue(part=part, step_index=1 if part == "derivation" else None,
                    claim="Synthetic review finds a repairable inference error." if part == "derivation" else
                          "A short legitimate route makes the target routine.",
                    evidence="The controlled first draft uses the wrong intermediate equation.",
                    repair_requirement="Correct the affected inference while preserving the locked target.")
                    for part in ("derivation", "target_depth")]
            return review, salt, "fixture"
        if schema is Question:
            return candidate.item, salt, "fixture"
        if schema is BottleneckReview:
            return mask_review(), salt, "fixture"
        if schema is CertificateAudit:
            assert data["correctness_basis"] == basis
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    target = {"category": candidate.item.category, "subtopic": candidate.item.subtopic}
    if basis == "self_verified":
        with pytest.raises(AuthoringRejected, match="target_depth_rejected"):
            asyncio.run(pipe.generate(source, None, target, 0, 0))
        assert proof_reviews == 1
        return
    result = asyncio.run(pipe.generate(source, None, target, 0, 0))
    assert proof_reviews == 2 and result.authoring_complete
    assert result.known_result == candidate.known_result
    assert result.solution.answer == candidate.known_result.answer
    assert not result.trials
    assert asyncio.run(pipe._revalidate_verifier_question(result))
    author_requests = [data for schema, data in requests if schema in (Question, Audit)]
    assert len(author_requests) == 3  # 题面生成、初审、题面重审。
    for data in author_requests:
        assert data["known_result"] == result.known_result.model_dump()
        assert data["correctness_basis"] == basis
    packet = VerifierBuilder(pipe).packet(result, {source.id: source})
    assert packet["correctness_basis"] == basis
    assert packet["initial_golden"]["known_result"] == result.known_result.model_dump()


@pytest.mark.parametrize("basis", ["self_verified", "paper_grounded"])
@pytest.mark.parametrize("concrete_error", [False, True])
def test_post_audit_preserves_mode_but_concrete_defects_always_block(
        tmp_path, settings, source, candidate, basis, concrete_error):
    settings.correctness_basis = candidate.correctness_basis = basis
    _pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    root = RootPlan(name="control", source_id=source.id, category=candidate.item.category,
                    subtopic=candidate.item.subtopic, focus="A synthetic post-audit contract control.")
    experiment = QuestionExperiment(settings, store, "post", "pilot", tmp_path / "post", gateway,
                                    plan=ExperimentPlan(roots=[root]), pricing=pricing())
    audit = candidate.audit.model_copy(update={"substantive_delta": False, "no_shortcut": False,
        "valid": not concrete_error, "reason": "A concrete contradiction." if concrete_error else
        "Cited source application accepted; subjective depth is not established."})

    async def call(role, prompt, salt, schema=None, **kwargs):
        assert role == "auditor" and schema is Audit
        assert prompt.startswith(experiment.prompts["audit"])
        assert "Independently re-derive the answer from scratch" not in prompt
        data = json.loads(prompt.splitlines()[-1])
        assert data["correctness_basis"] == basis
        assert data["known_result"] is None
        return audit, salt, "fixture"

    gateway.call = call
    passed = asyncio.run(experiment.final_audit(candidate))
    assert passed is (basis == "paper_grounded" and not concrete_error)
    assert (candidate.status == "rejected") is not passed


