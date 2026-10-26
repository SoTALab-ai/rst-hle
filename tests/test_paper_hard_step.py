"""论文硬步口径：只有具体错误阻断；不确定、未覆盖与深度意见只记录。"""
import asyncio
import json

import pytest
from conftest import passing_consistency
from pydantic import ValidationError
from test_qc import setup_pipeline

from diamond_factory.config import load
from diamond_factory.consistency import has_valid_consistency
from diamond_factory.models import (
    Audit,
    BlueprintAudit,
    BlueprintIssue,
    BottleneckReview,
    CertificateAttempt,
    CertificateAudit,
    ConsistencyAssessment,
    ConsistencyAudit,
    ConsistencyCheck,
    IndependentSolution,
    SubstitutionReport,
    digest,
)
from diamond_factory.pipeline import HARD_STEP_PROMPTS, Pipeline
from diamond_factory.store import Store

PG = "paper_grounded"


def check(verdict):
    return ConsistencyCheck(verdict=verdict, reasoning="Specific reasoning for this verdict.",
                            witness="Concrete witness or the exact unverified step.")


def review_with(verdict):
    review = passing_consistency()
    review.reference_answer = check(verdict)
    return review


def report(verdict):
    return SubstitutionReport(verdict=verdict, results=[], uncovered_outputs=[] if verdict == "pass" else ["x"],
                              reason="Fixture substitution report.")


def assessment(review, certificate_faithful=True, substitution="pass", attempts=()):
    certificate = CertificateAudit(faithful_translation=certificate_faithful, reason="Fixture certificate.",
                                   checks=[])
    value = ConsistencyAssessment(input_sha256="i", prompt_hashes={}, model_config_sha256="m",
        independent_solution=IndependentSolution(outcome="uncertain", derivation=["step one", "stopped"],
                                                 answer="unknown", issues=[]),
        audit=review, independent_request="a", review_request="b", independent_actual_model="m",
        review_actual_model="m", substitution=report(substitution), certificate_audit=certificate)
    value.certificate_attempts = list(attempts)
    if attempts:
        value.certificate_request = attempts[-1].request_id
    return value


@pytest.mark.parametrize("verdict, default, grounded", [("pass", True, True), ("uncertain", False, True),
                                                       ("fail", False, False)])
def test_consistency_review_blocks_only_concrete_failures(verdict, default, grounded):
    review = review_with(verdict)
    assert review.passed() is default
    assert review.passed(basis=PG) is grounded
    assert bool(review.failures(basis=PG)) is (verdict == "fail")


@pytest.mark.parametrize("verdict, default, grounded", [("pass", True, True), ("unsupported", False, True),
                                                       ("fail", False, False)])
def test_uncovered_substitution_is_recorded_but_counterexample_blocks(verdict, default, grounded):
    assert report(verdict).passed() is default
    assert report(verdict).passed(basis=PG) is grounded


def test_unfaithful_certificate_counts_as_uncovered_only_under_paper_basis():
    value = assessment(passing_consistency(), certificate_faithful=False, substitution="unsupported")
    assert not value.passed()
    assert value.passed(basis=PG)


def test_earlier_faithful_counterexample_still_blocks_paper_basis():
    failing = CertificateAttempt(certificate=CertificateAudit(faithful_translation=True,
        reason="Faithful translation of the original constraint.", checks=[]), substitution=report("fail"),
        request_id="first", actual_model="m", anchor_request="", anchor_actual_model="")
    final = CertificateAttempt(certificate=CertificateAudit(faithful_translation=True,
        reason="Fixture certificate.", checks=[]), substitution=report("pass"),
        request_id="second", actual_model="m", anchor_request="", anchor_actual_model="")
    value = assessment(passing_consistency(), attempts=[failing, final])
    assert not value.passed(basis=PG)


def test_mask_leak_stays_strict_while_depth_opinions_are_advisory():
    def mask(**verdicts):
        base = {name: "pass" for name in BottleneckReview.check_names()}
        base.update(verdicts)
        return BottleneckReview(step_id="key", **{name: check(v) for name, v in base.items()})

    advisory = mask(selection_supported="fail", necessary="fail", recoverable="uncertain")
    assert not advisory.passed() and advisory.passed(basis=PG)
    assert advisory.blocking_checks(basis=PG) == []
    for name in ("hidden", "not_answer_only"):
        assert not mask(**{name: "uncertain"}).passed(basis=PG)
    concrete = mask(recoverable="fail")
    assert not concrete.passed(basis=PG) and concrete.blocking_checks(basis=PG) == ["recoverable"]


def test_question_audit_ignores_depth_flags_but_not_missing_information():
    audit = Audit(valid=True, self_contained=True, uniquely_answerable=True, answer_supported=True,
                  substantive_delta=False, category_correct=True, no_shortcut=False,
                  reason="Routine route recorded; no concrete defect.")
    assert not audit.passed() and audit.passed(basis=PG)
    missing = audit.model_copy(update={"self_contained": False})
    assert not missing.passed(basis=PG)


def test_blueprint_depth_issue_is_deferred_but_scientific_issue_blocks():
    issue = BlueprintIssue(part="target_depth", claim="The target may be routine.",
                           evidence="A short route through a standard lemma exists.",
                           repair_requirement="Recorded only; measure the solver.")
    audit = BlueprintAudit(valid=True, answer_correct=True, derivation_complete=True, source_grounded=True,
                           reason="Science checked.", reviewed_step_count=2, issues=[issue])
    assert not audit.passed() and audit.passed(defer_depth=True)
    wrong = audit.model_copy(update={"answer_correct": False})
    assert not wrong.passed(defer_depth=True)


def test_paper_basis_config_loads_and_rejects_depth_adversary():
    settings = load("configs/library-paper-hard-step.toml")
    assert settings.correctness_basis == PG and settings.require_verifier_adversary
    with pytest.raises(ValidationError):
        type(settings).model_validate({**settings.model_dump(), "require_difficulty_adversary": True,
                                       "difficulty_references_path": "configs/difficulty-reference-cards.json"})


def test_paper_reference_check_replaces_independent_proof_prompt_only_under_paper_basis(tmp_path):
    settings = load("configs/library-paper-hard-step.toml")
    baseline = load("configs/library-source-inspired.toml")
    grounded = Pipeline(settings, Store(tmp_path / "a.sqlite"), "a", "pilot", tmp_path / "a")
    plain = Pipeline(baseline, Store(tmp_path / "b.sqlite"), "b", "pilot", tmp_path / "b")
    for name in HARD_STEP_PROMPTS:
        if name == "blueprint_audit":
            assert grounded.prompts[name] == plain.prompts["hardstep_" + name]
            assert "In advisory mode the paper is INSPIRATION" not in grounded.prompts[name]
            assert "Independently verify EVERY entry" not in grounded.prompts[name]
        else:
            assert grounded.prompts[name].startswith(plain.prompts[name])
            assert grounded.prompts[name].endswith(plain.prompts["hardstep_" + name])
        assert "PAPER HARD-STEP MODE" not in plain.prompts[name]
    assert grounded.fingerprint != plain.fingerprint


@pytest.mark.parametrize("basis", ["self_verified", PG])
def test_blueprint_request_explicitly_binds_paper_reference_scope(tmp_path, settings, source, candidate, basis):
    from diamond_factory.models import StepwiseBlueprintAudit

    settings = settings.model_copy(update={"correctness_basis": basis,
                                          "require_stepwise_blueprint_review": True})
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    payloads = []

    async def call(role, prompt, salt, schema=None, **kw):
        payloads.append(json.loads(prompt.splitlines()[-1]))
        return StepwiseBlueprintAudit(valid=True, answer_correct=True, derivation_complete=True,
            source_grounded=True, reason="Reference application checked without independently proving the paper.",
            reviewed_step_count=len(candidate.solution.derivation), issues=[]), "review", "gpt-6-luna"

    gateway.call = call
    result, _, _ = asyncio.run(pipe.review_blueprint(candidate.solution, source.model_dump(), {}, "check"))
    assert result.passed(defer_depth=basis == PG)
    data = payloads[0]
    if basis == PG:
        assert data["correctness_basis"] == PG
        assert data["review_scope"] == "paper_reference_and_premises"
        assert "not independent proofs" in data["step_indexing"]
    else:
        assert "correctness_basis" not in data and "review_scope" not in data
        assert data["step_indexing"].endswith("inspect every entry.")


def grounded_pipeline(tmp_path, settings, source, candidate, verdict):
    settings = settings.model_copy(update={"correctness_basis": PG})
    candidate.correctness_basis = PG
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        if schema is ConsistencyAudit:
            gateway.calls.append((role, prompt, salt))
            return review_with(verdict), digest(salt), "gpt-6-luna"
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    return pipe, store, gateway


def test_uncertain_review_reaches_solver_under_paper_basis(tmp_path, settings, source, candidate):
    pipe, store, gateway = grounded_pipeline(tmp_path, settings, source, candidate, "uncertain")
    result = asyncio.run(pipe.evaluate(candidate))
    assert result.status != "rejected"
    assert any(role == "solver" for role, _, _ in gateway.calls)
    assert has_valid_consistency(store.candidates()[0], {source.id: source})
    assert "PAPER HARD-STEP MODE" in next(p for r, p, s in gateway.calls if s.endswith(":review"))


def test_concrete_failure_still_blocks_solver_under_paper_basis(tmp_path, settings, source, candidate):
    pipe, _store, gateway = grounded_pipeline(tmp_path, settings, source, candidate, "fail")
    result = asyncio.run(pipe.evaluate(candidate))
    assert result.status == "rejected"
    assert not any(role in ("solver", "judge") for role, _, _ in gateway.calls)


def test_candidate_from_other_basis_requires_new_version(tmp_path, settings, source, candidate):
    settings = settings.model_copy(update={"correctness_basis": PG})
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    result = asyncio.run(pipe.evaluate(candidate))
    assert result.status == "rejected"
    assert "correctness_basis_changed_requires_new_version" in result.reasons
    assert not any(role == "solver" for role, _, _ in gateway.calls)
