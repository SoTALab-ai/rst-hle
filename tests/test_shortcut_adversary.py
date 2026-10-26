"""新协议允许浅种子先增强；验证真实短路、科学门禁和恢复，不把mock当难度证据。"""
import asyncio
import json

import pytest
from test_bottleneck import prepare
from test_difficulty_adversary import DepthGateway, challenge, generate
from test_qc import setup_pipeline

from diamond_factory.config import load
from diamond_factory.difficulty import construction_ready
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    BlueprintIssue,
    DifficultyChallenge,
    DifficultyProposal,
    ShortcutAttack,
    ShortcutRecheck,
    StepwiseBlueprintAudit,
    digest,
)
from diamond_factory.pipeline import Pipeline


class ShortcutGateway(DepthGateway):
    def __init__(self, candidate, design=None, question=None):
        super().__init__(candidate, design, question)
        self.blind_packets = []
        self.diagnostic_verdict = "too_easy"
        self.early_depth = False
        self.fail_diagnostic = False
        self.omit_old_route = False

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        data = json.loads(prompt.splitlines()[-1]) if schema in (ShortcutAttack, DifficultyChallenge) else {}
        if schema is ShortcutAttack:
            if salt not in self.cache:
                self.calls.append((role, prompt, salt))
                self.blind_packets.append(data)
                self.cache[salt] = ShortcutAttack(outcome="solved", derivation=["Compute the finite-tree count."],
                    answer="36", reason="Synthetic blind route used to test input separation and persistence."), digest(salt), "gpt-6-luna"
            return self.cache[salt]
        if schema is DifficultyChallenge and ":post-shortcut-depth:" in salt:
            if self.fail_diagnostic:
                self.fail_diagnostic = False
                raise ModelResponseError(role, digest(salt), "deadline_exceeded")
            self.calls.append((role, prompt, salt))
            return challenge(self.diagnostic_verdict), digest(salt), "gpt-6-luna"
        value, rid, model = await super().call(role, prompt, salt, schema, **kwargs)
        if schema is DifficultyProposal:
            value = value.model_copy(update={"public_task": self.candidate.item.question})
            self.cache[salt] = value, rid, model
        if schema is DifficultyChallenge and "prior_shortcut" in data:
            assert set(data["blind_attack_inputs"]) in ({"stage", "question"},
                                                       {"stage", "question", "required_outputs"})
            value = value.model_copy(deep=True)
            value.attack_validity = "correct"
            value.route_rechecks = [ShortcutRecheck(route_id=r["route_id"], verdict="blocked",
                evidence="Controlled recheck of the changed public task, not a scientific closure proof.")
                for r in data["prior_shortcut"]]
            if self.omit_old_route and value.route_rechecks:
                value.route_rechecks.pop()
            self.cache[salt] = value, rid, model
        if schema is StepwiseBlueprintAudit and self.early_depth:
            value = value.model_copy(deep=True)
            value.issues.append(BlueprintIssue(part="target_depth", claim="Synthetic routine route exists.",
                evidence="Controlled early-depth opinion; all scientific truth fields remain true.",
                repair_requirement="Legacy terminate feedback must not veto the new adversarial enhancement."))
            self.cache[salt] = value, rid, model
        return value, rid, model


def prepared(tmp_path, source, candidate, design=None, question=None):
    settings = load("configs/library-shortcut-adversary.toml")
    prepare(candidate, source)
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    pipe.gateway = ShortcutGateway(candidate.model_copy(deep=True), design, question)
    return pipe, store, pipe.gateway


def test_early_depth_is_not_a_veto_and_all_prior_routes_are_rechecked(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate,
        design=["too_easy", "too_easy", "sufficient"], question=["too_easy", "sufficient"])
    gateway.early_depth = True
    result = generate(pipe, source)
    assert result.status == "quality_seed" and construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)
    history = result.difficulty_origin.proposal_history
    assert len(history) == 3 and len(history[-1].challenge.route_rechecks) == 2
    assert len(result.difficulty_rounds[-1].challenge.route_rechecks) == 3
    packets = [json.loads(prompt.splitlines()[-1]) for _, prompt, salt in gateway.calls
               if salt.endswith(":author") and ":depth-design:" in salt]
    assert len(packets[-1]["prior_versions"]) == 2
    assert result.difficulty_diagnostic.assessment.verdict == "too_easy"
    first_mask = next(i for i, (_, _, salt) in enumerate(gateway.calls) if ":bottleneck:" in salt)
    last_attack = max(i for i, (_, _, salt) in enumerate(gateway.calls) if ":depth-question:" in salt
                     and not salt.endswith("audit"))
    assert first_mask > last_attack
    for packet in gateway.blind_packets:
        assert set(packet) in ({"stage", "question"}, {"stage", "question", "required_outputs"})
        assert packet["question"] in [r.item.question for r in result.difficulty_rounds] or packet["question"] == candidate.item.question
    assert not any(role == "solver" for role, _, _ in gateway.calls)


def test_post_adversary_depth_is_advisory_and_glm_still_measures(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    result = generate(pipe, source)
    assert result.difficulty_diagnostic.assessment.verdict == "too_easy"
    measured = asyncio.run(pipe.evaluate(result))
    assert len(measured.trials) == 4 and measured.reasons == ["too_easy:first_four_all_correct"]
    before = len(gateway.calls)
    assert generate(pipe, source).id == result.id and len(gateway.calls) == before


def test_missing_historical_shortcut_cannot_be_approved(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate, question=["too_easy", "sufficient"])
    gateway.omit_old_route = True
    with pytest.raises(ModelResponseError, match="shortcut_route_coverage_incomplete"):
        generate(pipe, source)
    assert not any(":verifier:" in salt or role == "solver" for role, _, salt in gateway.calls)


@pytest.mark.parametrize("change", ["attack", "old_route", "diagnostic", "protocol", "strategy", "public_task"])
def test_attack_history_and_diagnostic_cannot_be_removed_or_reused_after_changes(tmp_path, source, candidate, change):
    pipe, _, _ = prepared(tmp_path, source, candidate, design=["too_easy", "sufficient"])
    result = generate(pipe, source)
    assert construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)
    if change == "attack": result.difficulty_rounds[-1].shortcut_attack = None
    elif change == "old_route": result.difficulty_origin.proposal_history.pop(0)
    elif change == "diagnostic": result.difficulty_diagnostic.input_sha256 = "changed"
    elif change == "public_task": result.difficulty_origin.selected.proposal.public_task = None
    elif change == "strategy": result.difficulty_origin.strategy = "depth_gate"
    else: pipe.settings.difficulty_adversary_mode = "depth_gate"
    assert not construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)


def test_resume_after_diagnostic_failure_reuses_all_prior_calls(tmp_path, source, candidate):
    pipe, store, gateway = prepared(tmp_path, source, candidate)
    gateway.fail_diagnostic = True
    with pytest.raises(ModelResponseError):
        generate(pipe, source)
    blind = len(gateway.blind_packets)
    result = generate(Pipeline(pipe.settings, store, pipe.run_id, pipe.mode, pipe.run_dir, gateway), source)
    assert result.status == "quality_seed" and len(gateway.blind_packets) == blind
    assert construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)


def test_scientific_error_is_not_erased_by_deferred_depth(tmp_path, source, candidate):
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is StepwiseBlueprintAudit:
            value.valid = False
            value.issues.append(BlueprintIssue(part="answer", claim="Controlled wrong gold in a test proposal.",
                evidence="Scientific failure remains blocking even when the target-depth opinion is deferred.",
                repair_requirement="Re-derive the provisional answer before any shortcut attack."))
        return value, rid, model

    gateway.call = call
    from diamond_factory.pipeline import AuthoringRejected
    with pytest.raises(AuthoringRejected):
        generate(pipe, source)
    assert not gateway.blind_packets and not any(role == "solver" for role, _, _ in gateway.calls)


def test_scientifically_valid_question_reaches_attack_despite_preliminary_shortcut_flag(tmp_path, source, candidate):
    from diamond_factory.models import Audit
    pipe, _, gateway = prepared(tmp_path, source, candidate, question=["too_easy", "sufficient"])
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is Audit and not gateway.revision_calls:
            value = value.model_copy(update={"no_shortcut": False, "substantive_delta": False})
        return value, rid, model

    gateway.call = call
    result = generate(pipe, source)
    assert len(result.difficulty_rounds) == 2 and result.difficulty_rounds[0].audit.no_shortcut is False
    assert result.status == "quality_seed" and construction_ready(result, {source.id: source}, pipe.settings, pipe.prompts)


def test_missing_exact_public_task_never_becomes_a_privileged_blind_attack(tmp_path, source, candidate):
    from diamond_factory.pipeline import AuthoringRejected
    pipe, _, gateway = prepared(tmp_path, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is DifficultyProposal:
            value = value.model_copy(update={"public_task": None})
        return value, rid, model

    gateway.call = call
    with pytest.raises(AuthoringRejected):
        generate(pipe, source)
    assert not gateway.blind_packets
