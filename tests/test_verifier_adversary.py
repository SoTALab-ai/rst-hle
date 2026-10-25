import asyncio
import json

import pytest
from conftest import trials
from test_qc import FakeGateway, setup_pipeline

from diamond_factory.difficulty import construction_ready
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    CertificateAnchors,
    CertificateAudit,
    Judgment,
    VerifierChallenges,
    VerifierDecision,
    VerifierOracle,
    VerifierQuestionRepair,
    VerifierReview,
    VerifierSpec,
    digest,
)
from diamond_factory.pipeline import QualityRejected
from diamond_factory.verifier import has_valid_verifier, protocol_hash, round_errors


def check(verdict="pass"):
    return {"verdict": verdict, "reasoning": "Concrete independent fixture reasoning.",
            "witness": "The question determines a tree with 37 vertices and 36 edges."}


class Adversary(FakeGateway):
    def __init__(self, candidate, mode="pass"):
        super().__init__(candidate)
        self.mode = mode
        self.cache = {}
        self.failed_once = False

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        if schema not in (VerifierSpec, VerifierChallenges, VerifierOracle, VerifierDecision,
                          VerifierReview, VerifierQuestionRepair) and ":hle-control:" not in salt:
            return await super().call(role, prompt, salt, schema, **kwargs)
        if salt in self.cache:
            return self.cache[salt]
        self.calls.append((role, prompt, salt))
        data = json.loads(prompt.splitlines()[-1]) if ":hle-control:" not in salt else None
        index = int(salt.split(":verifier:")[1].split(":")[2])
        bad = self.mode in ("false_kill", "always_false_kill") and (index == 0 or self.mode == "always_false_kill")
        question_issue = self.mode == "question_repair" and index == 0
        if schema is VerifierSpec:
            assert data["initial_golden"]["solution"]["derivation"]
            value = VerifierSpec.model_validate({"requirements": [{"target_output": "edge count",
                "question_anchor": "37 vertices", "acceptance_rule": "Return the tree edge count, accepting equivalents.",
                "equivalent_forms": ["36", "thirty-six"]}], "conditions": [{"condition": "finite tree",
                "kind": "inferable", "question_anchor": "without cycles", "justification": "Connected and acyclic is exactly a tree."}],
                "interpretation": "Reject spelled out numbers incorrectly" if bad else "Accept all exact integer equivalents."})
        elif schema is VerifierChallenges:
            value = VerifierChallenges.model_validate({"cases": [{"id": name, "kind": kind,
                "response": response, "target_output_index": 0, "rationale": "Independent control of the exact tree count."}
                for name, kind, response in (("word", "equivalent", "thirty-six"), ("number", "equivalent", "Final: 36"),
                                            ("wrong", "invalid", "37"), ("missing", "invalid", "No answer"))],
                "suspected_question_issues": ["Target convention needs clarification."] if question_issue else []})
        elif schema is VerifierOracle:
            assert "verifier" not in data and "kind" not in str(data["responses"])
            ids = [r["id"] for r in data["responses"]]
            value = VerifierOracle.model_validate({"cases": [{"id": cid,
                "verdict": "yes" if cid in ("golden", "word", "number") else "no",
                "reasoning": "The literal question determines the single integer 36."} for cid in ids],
                "question_complete": check("fail" if question_issue else "pass"),
                "target_unambiguous": check(),
                "golden_valid": check("fail" if self.mode == "bad_gold" else "pass"),
                "hidden_steps_recoverable": check()})
        elif schema is VerifierDecision:
            valid = data["response"] in ("36", "thirty-six", "Final: 36")
            if bad and data["response"] == "thirty-six": valid = False
            value = VerifierDecision(correct="yes" if valid else "no", reasoning="Applied the literal integer acceptance rule.")
        elif schema is Judgment:
            valid = any(marker in prompt for marker in ("[response]: 36", "[response]: thirty-six", "[response]: Final: 36"))
            value = Judgment(extracted_final_answer="36" if valid else "37", correct="yes" if valid else "no",
                             reasoning="Official template matches the control answer.", confidence=100, strict=True)
        elif schema is VerifierReview:
            if self.mode == "interrupt" and not self.failed_once:
                self.failed_once = True
                raise ModelResponseError(role, digest(salt), "deadline_exceeded")
            action = "repair_verifier" if bad else "repair_question" if question_issue else "pass"
            value = VerifierReview(action=action, question_verifier_consistent=check(), requested_outputs_covered=check(),
                no_hidden_extra_conditions=check(), feedback="Repair the concrete control defect without changing the locked golden.")
        else:
            value = VerifierQuestionRepair(question=self.candidate.item.question + " The target is the exact edge count.",
                assumptions=self.candidate.item.assumptions, rationale=self.candidate.item.rationale,
                change_reason="Clarify the target without exposing the hidden theorem.")
        result = value, digest(salt), "gpt-6-luna"
        self.cache[salt] = result
        return result


def prepared(tmp_path, settings, source, candidate, mode="pass"):
    settings.require_verifier_adversary = True
    # 这些专门的旧协议测试保留题面修复能力；新协议默认冻结题面。
    if mode == 'question_repair':
        settings.freeze_question_at_verifier = False
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    pipe.gateway = Adversary(candidate.model_copy(deep=True), mode)
    return pipe, store, pipe.gateway


def test_controls_include_locked_golden_and_hidden_proof_without_leaking_to_blind_solve(
        tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    assert asyncio.run(pipe.preflight(candidate))
    assert has_valid_verifier(candidate, {source.id: source})
    assert construction_ready(candidate, {source.id: source}, settings, pipe.prompts)
    result = candidate.verifier_assessment.versions[-1].result
    assert {o.case_id for o in result.observations} == {"golden", "word", "number", "wrong", "missing"}
    blind = [json.loads(p.splitlines()[-1]) for _, p, s in gateway.calls if s.endswith(":solve")]
    assert blind == [{"question": candidate.item.question}]
    assert not any(role == "solver" for role, _, _ in gateway.calls)
    before = len(gateway.calls)
    assert asyncio.run(pipe.preflight(candidate)) and len(gateway.calls) == before
    changed = dict(pipe.prompts, verifier_apply=pipe.prompts["verifier_apply"] + " changed")
    assert not has_valid_verifier(candidate, {source.id: source}, expected_protocol=protocol_hash(changed, settings))
    candidate.item.question += " Changed question."
    assert not has_valid_verifier(candidate, {source.id: source})


def test_false_rejection_repairs_only_verifier(tmp_path, settings, source, candidate):
    pipe, _, _ = prepared(tmp_path, settings, source, candidate, "false_kill")
    original = candidate.item.model_copy(deep=True)
    assert asyncio.run(pipe.preflight(candidate))
    versions = candidate.verifier_assessment.versions
    assert len(versions) == 2 and all(v.item == original for v in versions)
    assert "false_rejection:word" in round_errors(versions[0].result, original)
    assert not round_errors(versions[1].result, original)


def test_two_repairs_max_and_bad_gold_never_replaced(tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate, "always_false_kill")
    assert not asyncio.run(pipe.preflight(candidate))
    assert len(candidate.verifier_assessment.versions) == 3
    assert candidate.item.answer == candidate.solution.answer == "36"
    assert not any(role == "solver" for role, _, _ in gateway.calls)
    fresh = candidate.model_copy(deep=True)
    fresh.verifier_assessment = None; fresh.status = "quality_seed"
    pipe.gateway = Adversary(fresh, "bad_gold")
    assert not asyncio.run(pipe.preflight(fresh))
    assert len(fresh.verifier_assessment.versions) == 1
    assert "golden" in fresh.reasons[0]


def test_question_patch_preserves_answer_and_repeats_all_correctness_checks(tmp_path, settings, source, candidate):
    pipe, store, gateway = prepared(tmp_path, settings, source, candidate, "question_repair")
    original = candidate.model_copy(deep=True)
    assert asyncio.run(pipe.preflight(candidate))
    versions = candidate.verifier_assessment.versions
    assert len(versions) == 2 and versions[0].item == original.item and versions[1].item == candidate.item
    assert candidate.item.question != original.item.question
    assert candidate.item.answer == original.item.answer and candidate.solution == original.solution
    assert sum(s.endswith(":solve") for _, _, s in gateway.calls) == 2
    assert sum(s.endswith(":certificate") for _, _, s in gateway.calls) == 2
    assert any(":verifier-question-revalidation:" in s for _, _, s in gateway.calls)
    assert not versions[-1].revalidation_pending
    assert has_valid_verifier(store.candidates()[0], {source.id: source})


def test_infrastructure_failure_resumes_checkpoint_without_rebuying_completed_calls(
        tmp_path, settings, source, candidate):
    pipe, store, gateway = prepared(tmp_path, settings, source, candidate, "interrupt")
    with pytest.raises(ModelResponseError): asyncio.run(pipe.preflight(candidate))
    checkpoint = store.candidates()[0]
    assert checkpoint.verifier_assessment.status == "pending" and not checkpoint.trials
    count = sum(s.endswith(":draft") for _, _, s in gateway.calls)
    assert asyncio.run(pipe.preflight(checkpoint))
    assert sum(s.endswith(":draft") for _, _, s in gateway.calls) == count
    assert not any(role == "solver" for role, _, _ in gateway.calls)


def test_sample_bypass_cannot_modify_a_measured_item(tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    candidate.trials = trials(4)
    original = candidate.model_copy(deep=True)
    with pytest.raises(QualityRejected): asyncio.run(pipe.sample(candidate))
    assert candidate.item == original.item and candidate.trials == original.trials
    assert not any(role == "solver" for role, _, _ in gateway.calls)


def test_tampered_or_missing_controls_and_unknown_results_fail_closed(tmp_path, settings, source, candidate):
    pipe, _, _ = prepared(tmp_path, settings, source, candidate)
    assert asyncio.run(pipe.preflight(candidate))
    result = candidate.verifier_assessment.versions[-1].result
    altered = result.model_copy(deep=True)
    altered.observations[-1].oracle = altered.observations[-1].oracle.model_copy(update={"verdict": "yes"})
    assert "oracle_observation_mapping_mismatch" in round_errors(altered, candidate.item)
    altered = result.model_copy(deep=True)
    altered.observations = altered.observations[:3]
    assert "negative_output_coverage_incomplete" in round_errors(altered, candidate.item)
    altered = result.model_copy(deep=True)
    altered.observations[1].verifier.correct = "uncertain"
    assert "unresolved_control:word" in round_errors(altered, candidate.item)
    candidate.solution.derivation.append("An unreviewed change to the hidden proof.")
    assert not has_valid_verifier(candidate, {source.id: source})


def test_hle_mismatch_cannot_be_overridden_by_local_verifier_repair(tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate, "false_kill")
    original = gateway.call

    async def wrong_hle(role, prompt, salt, schema=None, **kwargs):
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if ":hle-control:word" in salt:
            value = value.model_copy(update={"correct": "no"})
        return value, rid, model

    gateway.call = wrong_hle
    assert not asyncio.run(pipe.preflight(candidate))
    assert len(candidate.verifier_assessment.versions) == 1
    assert "hle_disagreement" in candidate.reasons[0]


def test_pending_question_validation_resumes_before_accepting_old_audit(tmp_path, settings, source, candidate):
    pipe, store, gateway = prepared(tmp_path, settings, source, candidate, "question_repair")
    original = pipe._revalidate_verifier_question

    async def interrupted(c):
        raise ModelResponseError("auditor", "in-flight", "deadline_exceeded")

    pipe._revalidate_verifier_question = interrupted
    with pytest.raises(ModelResponseError): asyncio.run(pipe.preflight(candidate))
    saved = store.candidates()[0]
    assert saved.verifier_assessment.versions[-1].revalidation_pending
    assert saved.consistency is None and not saved.trials
    pipe._revalidate_verifier_question = original
    assert asyncio.run(pipe.preflight(saved))
    assert not saved.verifier_assessment.versions[-1].revalidation_pending
    assert sum(s.endswith(":solve") for _, _, s in gateway.calls) == 2


def test_literal_anchor_failure_has_bounded_format_repair_without_semantic_change(tmp_path, settings, source, candidate):
    pipe, _, gateway = prepared(tmp_path, settings, source, candidate)
    original = gateway.call
    bad_request = []
    saved_rules = []

    async def bad_anchor(role, prompt, salt, schema=None, **kwargs):
        from diamond_factory.models import VerifierLocatorPatches
        if schema is VerifierLocatorPatches:
            gateway.calls.append((role, prompt, salt))
            data = json.loads(prompt.splitlines()[-1])
            assert data['invalid_locator_indices'] == [['conditions', 0]]
            return VerifierLocatorPatches.model_validate({'patches': [{'group': 'conditions', 'index': 0,
                'question_anchor': '37 vertices'}]}), digest(salt), 'gpt-6-luna'
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is VerifierSpec and salt.endswith(":draft"):
            value = value.model_copy(deep=True)
            saved_rules.append(value.conditions[0].model_dump(exclude={'question_anchor'}))
            value.conditions[0].question_anchor = "an invented paraphrase of the conditions"
            bad_request.append(rid)
        elif schema is VerifierSpec:
            data = json.loads(prompt.splitlines()[-1])
            assert data["format_repair"]["exact_required_outputs"] == candidate.item.required_outputs
            assert "FORMAT REPAIR ONLY" in data["format_repair"]["instruction"]
        return value, rid, model

    gateway.call = bad_anchor
    assert asyncio.run(pipe.preflight(candidate))
    assert len(bad_request) == 1 and len(candidate.verifier_assessment.versions) == 1
    assert any(":literal-locators:" in s for _, _, s in gateway.calls)
    version = candidate.verifier_assessment.versions[0]
    from diamond_factory.verifier import anchor_repairs_valid
    assert anchor_repairs_valid(version)
    assert version.anchor_repairs[-1].request_id
    original_rule = version.spec.conditions[0].model_dump(exclude={'question_anchor'})
    assert original_rule == saved_rules[0]


def test_initial_audit_ambiguity_can_enter_question_repair_but_not_skip_audit(tmp_path, settings, source, candidate):
    pipe, _, _ = prepared(tmp_path, settings, source, candidate, "question_repair")
    # 网关保留独立合法审查；旧题的待澄清结论不能让对抗入口永远不可达。
    candidate.audit = candidate.audit.model_copy(update={"uniquely_answerable": False})
    assert asyncio.run(pipe.preflight(candidate))
    assert candidate.audit.passed() and len(candidate.verifier_assessment.versions) == 2


@pytest.mark.parametrize("bad_math", [False, True])
def test_certificate_anchor_recovery_cannot_edit_math_or_hide_a_counterexample(
        tmp_path, settings, source, candidate, bad_math):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call
    originals = []

    async def anchors(role, prompt, salt, schema=None, **kwargs):
        if schema is CertificateAnchors:
            data = json.loads(prompt.splitlines()[-1])
            assert data["bad_check_indices"] == [0]
            return CertificateAnchors.model_validate({"patches": [{"check_index": 0,
                "question_anchor": "37 vertices", "answer_anchor": "36", "target_output_index": 0}]}), digest(salt), "gpt-6-luna"
        value, rid, model = await original(role, prompt, salt, schema, **kwargs)
        if schema is CertificateAudit:
            value = value.model_copy(deep=True)
            value.checks[0].question_anchor = "37 Vertices"
            if bad_math: value.checks[0].rhs = "gold+1"
            originals.append(value.checks[0])
        return value, rid, model

    gateway.call = anchors
    assert asyncio.run(pipe.preflight(candidate)) == (not bad_math)
    after = candidate.consistency.certificate_audit.checks[0]
    assert after.model_dump(exclude={"question_anchor"}) == originals[0].model_dump(exclude={"question_anchor"})
    assert candidate.consistency.certificate_anchor_request
    assert candidate.consistency.substitution.verdict == ("fail" if bad_math else "pass")
