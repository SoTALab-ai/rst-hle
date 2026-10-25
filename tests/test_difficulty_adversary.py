import asyncio
import json

import pytest
from conftest import trials
from pydantic import ValidationError
from test_bottleneck import mask_review, prepare
from test_difficulty import step_review
from test_qc import setup_pipeline
from test_verifier_adversary import Adversary

from diamond_factory.config import load
from diamond_factory.difficulty import construction_ready
from diamond_factory.difficulty_adversary import DifficultyAdversary, has_valid_difficulty
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    BottleneckReview,
    DifficultyChallenge,
    DifficultyMaskRevision,
    DifficultyProposal,
    LockedProofDraft,
    LockedTaskDesign,
    Question,
    StepwiseBlueprintAudit,
    digest,
)
from diamond_factory.pipeline import AuthoringRejected, QualityRejected


def challenge(verdict="sufficient"):
    return DifficultyChallenge(verdict=verdict, shortest_correct_route=["Use the defining tree relation to obtain the count."],
        route_validity="verified", indispensable_proof_entries=[1],
        remaining_discoveries=["Synthetic test burden; this does not establish actual difficulty."],
        reference_comparison="Controlled protocol fixture, not a genuine comparison of mathematical depth.",
        feedback=["Controlled step-specific feedback; preserve all independent model premises."],
        reason="Synthetic independent decision used to test bounded control flow only.")


class DepthGateway(Adversary):
    def __init__(self, candidate, design=None, question=None):
        super().__init__(candidate)
        self.design_verdicts, self.question_verdicts = design or ["sufficient"], question or ["sufficient"]
        self.design_calls = self.question_calls = self.revision_calls = 0
        self.fail_question = False

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        if schema not in (DifficultyProposal, DifficultyChallenge, DifficultyMaskRevision,
                          StepwiseBlueprintAudit, BottleneckReview, Question, LockedProofDraft):
            return await super().call(role, prompt, salt, schema, **kwargs)
        if salt in self.cache:
            return self.cache[salt]
        self.calls.append((role, prompt, salt))
        data = json.loads(prompt.splitlines()[-1])
        if schema in (DifficultyProposal, LockedProofDraft):
            design = self.candidate.solution.design.model_dump(exclude={"target"})
            draft = LockedProofDraft(topic=self.candidate.solution.topic, additional_assumptions=[],
                derivation=list(self.candidate.solution.derivation), evidence=self.candidate.solution.evidence,
                reasoning_delta=self.candidate.solution.reasoning_delta + f" Proposal {data.get('revision', 'easing')}.",
                design=LockedTaskDesign.model_validate(design))
            value = DifficultyProposal(result=self.candidate.known_result, proof=draft) if schema is DifficultyProposal else draft
        elif schema is DifficultyChallenge:
            if data["stage"] == "before_lock":
                verdict = self.design_verdicts[min(self.design_calls, len(self.design_verdicts) - 1)]
                self.design_calls += 1
                assert data["reference_cards"]["cards"] and "question" not in data
            else:
                if self.fail_question:
                    self.fail_question = False
                    raise ModelResponseError(role, digest(salt), "deadline_exceeded")
                verdict = self.question_verdicts[min(self.question_calls, len(self.question_verdicts) - 1)]
                self.question_calls += 1
                assert data["independent_solution"]["answer"] == "36"
            value = challenge(verdict)
        elif schema is DifficultyMaskRevision:
            self.revision_calls += 1
            value = DifficultyMaskRevision(question=data["item"]["question"] + " Count each edge once.",
                concealed_proof_entries=[1], reasoning="Synthetic revised question tests revalidation only.",
                recoverability="The same finite-tree assumptions determine the unchanged locked count.")
        elif schema is StepwiseBlueprintAudit:
            value = step_review(count=len(data["solution"]["derivation"]))
        elif schema is BottleneckReview:
            value = mask_review()
        else:
            value = self.candidate.item.model_copy(deep=True)
        result = value, digest(salt), "gpt-6-luna"
        self.cache[salt] = result
        return result


def prepared(tmp_path, source, candidate, *, design=None, question=None):
    settings = load("configs/library-difficulty-adversary.toml")
    prepare(candidate, source)
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    pipe.gateway = DepthGateway(candidate.model_copy(deep=True), design, question)
    return pipe, store, pipe.gateway


def generate(pipe, source):
    return asyncio.run(pipe.generate(source, None, {"category": "Math", "subtopic": "组合与图论"}, 0, 0))


def test_both_stages_revise_then_freeze_and_revalidate_before_verifier(tmp_path, source, candidate):
    pipe, store, gateway = prepared(tmp_path, source, candidate,
        design=["too_easy", "sufficient"], question=["too_easy", "sufficient"])
    result = generate(pipe, source)
    assert result.authoring_complete and result.status == "quality_seed"
    assert gateway.design_calls == gateway.question_calls == 2 and gateway.revision_calls == 1
    assert result.item.answer == "36" and result.known_result == candidate.known_result
    assert result.solution == result.difficulty_origin.selected.proposal.proof.bind(result.known_result)
    assert len(result.difficulty_rounds) == 2
    assert result.difficulty_rounds[0].item != result.item
    assert result.difficulty_rounds[0].solution == result.solution
    assert construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)
    assert sum(s.endswith(":solve") for _, _, s in gateway.calls) == 2
    assert sum(s.endswith(":certificate") for _, _, s in gateway.calls) == 2
    first_verifier = next(i for i, (_, _, s) in enumerate(gateway.calls) if ":verifier:" in s)
    reviews = [i for i, (_, _, s) in enumerate(gateway.calls) if ":depth-question:" in s and not s.endswith("audit")]
    assert first_verifier > max(reviews)
    assert not any(role == "solver" for role, _, _ in gateway.calls)
    before = len(gateway.calls)
    loaded = store.candidates()[0]
    assert asyncio.run(pipe.preflight(loaded)) and len(gateway.calls) == before


def test_stage_one_uses_three_distinct_proposals_and_terminates_without_question(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate, design=["too_easy"])
    with pytest.raises(AuthoringRejected, match="difficulty_design_exhausted"):
        generate(pipe, source)
    assert gateway.design_calls == 3 and not gateway.question_calls
    assert not any(":depth-align:" in salt or role == "solver" for role, _, salt in gateway.calls)
    builder = DifficultyAdversary(pipe)
    ref = next(iter(pipe.difficulty_checkpoints.values()))
    state = builder.objects.get(ref["key"], ref["sha256"], ref["size"])
    assert state["status"] == "rejected" and len(state["versions"]) == 3
    assert len({v["proposal_id"] for v in state["versions"]}) == 3
    count = len(gateway.calls)
    with pytest.raises(AuthoringRejected):
        generate(pipe, source)
    assert len(gateway.calls) == count


def test_stage_two_limit_cannot_trigger_outer_author_rewrite(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate, question=["too_easy"])
    result = generate(pipe, source)
    assert result.status == "rejected" and result.reasons == ["difficulty_question_too_easy"]
    assert gateway.question_calls == 3 and gateway.revision_calls == 2
    assert sum(":depth-align:" in s and not s.endswith("audit") for _, _, s in gateway.calls) == 1
    assert not any(":verifier:" in s or role == "solver" for role, _, s in gateway.calls)


@pytest.mark.parametrize("stage", ["design", "question"])
def test_uncertainty_never_triggers_hardening_or_wrong_solver_grade(tmp_path, source, candidate, stage):
    pipe, _, gateway = prepared(tmp_path, source, candidate,
        design=["uncertain"] if stage == "design" else None,
        question=["uncertain"] if stage == "question" else None)
    if stage == "design":
        with pytest.raises(AuthoringRejected, match="unverified"):
            generate(pipe, source)
        assert gateway.design_calls == 1
    else:
        result = generate(pipe, source)
        assert result.status == "rejected" and "uncertain" in result.reasons[0]
    assert not gateway.revision_calls and not any(role == "solver" for role, _, _ in gateway.calls)


def test_resume_reuses_saved_science_and_prelock_checkpoint(tmp_path, source, candidate):
    pipe, store, gateway = prepared(tmp_path, source, candidate)
    gateway.fail_question = True
    with pytest.raises(ModelResponseError):
        generate(pipe, source)
    assert store.candidates()[0].consistency is not None and gateway.design_calls == 1
    count = sum(s.endswith(":solve") for _, _, s in gateway.calls)
    # 新进程从PG/SQLite run指针恢复，完整提案在CAS，已完成模型答复由网关封印缓存复用。
    from diamond_factory.pipeline import Pipeline
    resumed = Pipeline(pipe.settings, store, pipe.run_id, pipe.mode, pipe.run_dir, gateway)
    result = generate(resumed, source)
    assert result.status == "quality_seed" and gateway.design_calls == 1
    assert sum(s.endswith(":solve") for _, _, s in gateway.calls) == count


@pytest.mark.parametrize("change", ["question", "gold", "proof", "prompt", "model", "endpoint", "origin", "history"])
def test_changed_inputs_block_sampling_export_and_frozen_rewrite(tmp_path, source, candidate, monkeypatch, change):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    result = generate(pipe, source)
    if change == "question": result.item.question += " changed"
    elif change == "gold": result.item.answer = "37"
    elif change == "proof": result.solution.derivation[0] += " changed"
    elif change == "prompt": pipe.prompts["difficulty_challenge"] += " changed"
    elif change == "model": pipe.settings.consistency.reasoning_effort = "medium"
    elif change == "endpoint": monkeypatch.setenv(pipe.settings.consistency.base_url_env, "https://changed.example")
    elif change == "origin": result.difficulty_origin.selected.challenge.verdict = "too_easy"
    else: result.difficulty_rounds[0].parent_input_sha256 = "unrecorded-parent"
    before = len(gateway.calls)
    assert not construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)
    result.trials = trials(4)[:4]
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(result))
    assert len(gateway.calls) == before and len(result.trials) == 4


def test_external_candidate_without_origin_cannot_bypass_preflight(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    assert not asyncio.run(pipe.preflight(candidate))
    assert candidate.reasons == ["missing_or_stale_difficulty_origin"] and not gateway.calls


def test_unverified_routes_and_reference_completion_claims_are_rejected():
    value = challenge().model_dump()
    value["route_validity"] = "uncertain"
    with pytest.raises(ValidationError):
        DifficultyChallenge.model_validate(value)
    from pathlib import Path

    from diamond_factory.difficulty_adversary import ReferenceCards
    cards = json.loads(Path("configs/difficulty-reference-cards.json").read_text())
    cards["cards"][0]["measurement_complete"] = True
    with pytest.raises(ValidationError):
        ReferenceCards.model_validate(cards)


def test_checkpoint_corruption_refuses_recovery(tmp_path, source, candidate):
    pipe, _, _ = prepared(tmp_path, source, candidate)
    result = generate(pipe, source)
    assert has_valid_difficulty(result, {source.id: source}, DifficultyAdversary(pipe).protocol)
    pointer = next(iter(pipe.difficulty_checkpoints.values()))
    (DifficultyAdversary(pipe).objects.root / pointer["key"]).write_text("corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        asyncio.run(DifficultyAdversary(pipe).design(source,
            {"category": "Math", "subtopic": "组合与图论"}, result.id, None))


def test_resume_after_revised_science_keeps_old_versions_and_reuses_blind_solve(tmp_path, source, candidate):
    pipe, store, gateway = prepared(tmp_path, source, candidate, question=["too_easy", "sufficient"])
    original = gateway.call
    interrupted = False

    async def call(role, prompt, salt, schema=None, **kwargs):
        nonlocal interrupted
        if schema is DifficultyChallenge:
            data = json.loads(prompt.splitlines()[-1])
            if data["stage"] == "actual_question" and data["question"].endswith("Count each edge once.") and not interrupted:
                interrupted = True
                raise ModelResponseError(role, digest(salt), "deadline_exceeded")
        return await original(role, prompt, salt, schema, **kwargs)

    gateway.call = call
    with pytest.raises(ModelResponseError):
        generate(pipe, source)
    saved = store.candidates()[0]
    assert len(saved.difficulty_rounds) == 1 and saved.consistency is not None
    assert saved.difficulty_rounds[0].revision is not None
    blind_count = sum(s.endswith(":solve") for _, _, s in gateway.calls)
    from diamond_factory.pipeline import Pipeline
    resumed = Pipeline(pipe.settings, store, pipe.run_id, pipe.mode, pipe.run_dir, gateway)
    result = generate(resumed, source)
    assert result.status == "quality_seed" and len(result.difficulty_rounds) == 2
    assert gateway.revision_calls == 1
    assert sum(s.endswith(":solve") for _, _, s in gateway.calls) == blind_count


def test_scientific_failure_after_mask_revision_is_terminal_and_never_verifier(tmp_path, source, candidate):
    from diamond_factory.models import ConsistencyAudit, ConsistencyCheck

    pipe, store, gateway = prepared(tmp_path, source, candidate, question=["too_easy", "sufficient"])
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is ConsistencyAudit and gateway.revision_calls:
            value.reference_answer = ConsistencyCheck(verdict="fail",
                reasoning="Synthetic revised question does not establish the locked result.",
                witness="Controlled independent scientific failure, not a hardness observation.")
        return value, rid, model

    gateway.call = call
    result = generate(pipe, source)
    assert result.status == "rejected" and len(result.difficulty_rounds) == 1
    assert result.item.answer == "36" and result.authoring_complete
    assert not any(":verifier:" in s or role == "solver" for role, _, s in gateway.calls)
    # 即使在保存authoring_complete之前中断，已有失败轮也不能被外层重写清掉。
    result.authoring_complete = False
    store.save(result)
    before = len(gateway.calls)
    recovered = generate(pipe, source)
    assert recovered.difficulty_rounds == result.difficulty_rounds and len(gateway.calls) == before


def test_easing_child_is_reviewed_but_never_hardened(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    parent = generate(pipe, source)
    parent.trials = trials(0)
    pipe.store.save(parent)
    gateway.question_verdicts = ["too_easy"]
    before = gateway.revision_calls
    child = asyncio.run(pipe.generate(source, parent,
        {"category": "Math", "subtopic": "组合与图论"}, 1, 0))
    assert child.generation == 1 and child.difficulty_origin.root_id == parent.root_id
    assert child.status == "rejected" and child.reasons == ["difficulty_question_too_easy"]
    assert gateway.revision_calls == before and len(child.difficulty_rounds) == 1


def test_actual_first_four_correct_remains_closed_without_post_sampling_hardening(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    result = generate(pipe, source)
    evaluated = asyncio.run(pipe.evaluate(result))
    assert evaluated.reasons == ["too_easy:first_four_all_correct"] and len(evaluated.trials) == 4
    before = len(gateway.calls)
    assert generate(pipe, source).id == result.id
    assert len(gateway.calls) == before
