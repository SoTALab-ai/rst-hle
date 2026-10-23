import asyncio
import json

import pytest
from conftest import passing_consistency, trials
from pydantic import ValidationError
from test_experiment import PatternGateway, pricing
from test_qc import FakeGateway, setup_pipeline

from diamond_factory.consistency import QualityRejected, has_valid_consistency
from diamond_factory.experiment import ExperimentPlan, QuestionExperiment, RootPlan
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import (
    BlueprintAudit,
    CertificateAudit,
    ConsistencyAudit,
    ConsistencyCheck,
    IndependentSolution,
    Question,
    Solution,
    Submission,
    digest,
)
from diamond_factory.pipeline import AuthoringRejected


def defective_review(verdict="fail"):
    review = passing_consistency()
    review.reference_answer = ConsistencyCheck(verdict=verdict,
        reasoning="The gold contradicts the literal defining equation.",
        witness="Substitution into the defining matrix yields a nonzero residual.")
    return review


@pytest.mark.parametrize("verdict", ["fail", "uncertain"])
def test_failed_or_uncertain_preflight_blocks_solver_and_preserves_old_trials(
        tmp_path, settings, source, candidate, verdict):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    candidate.trials = trials(7)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        if schema is ConsistencyAudit:
            gateway.calls.append((role, prompt, salt))
            return defective_review(verdict), digest(salt), "gpt-6-luna"
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    before = candidate.model_dump()["trials"]
    result = asyncio.run(pipe.evaluate(candidate))
    assert result.status == "rejected" and result.model_dump()["trials"] == before
    assert not any(role in ("solver", "judge") for role, _, _ in gateway.calls)
    assert not has_valid_consistency(store.candidates()[0], {source.id: source})
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(candidate))


def test_independent_solve_sees_only_question_and_review_precedes_all_samples(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    asyncio.run(pipe.evaluate(candidate))
    solve, review, certificate = [c for c in gateway.calls if c[0] == "consistency"]
    assert json.loads(solve[1].split("\n")[-1]) == {"question": candidate.item.question}
    data = json.loads(review[1].split("\n")[-1])
    assert data["solution"]["answer"] == "36" and data["independent_solution"]["answer"] == "36"
    assert "trials" not in data and "audit" not in data and "parent" not in data
    certificate_data = json.loads(certificate[1].split("\n")[-1])
    assert "independent_solution" not in certificate_data and "trials" not in certificate_data
    assert gateway.calls.index(review) < next(i for i, c in enumerate(gateway.calls) if c[0] == "solver")
    assert all(prompt == candidate.item.question for role, prompt, _ in gateway.calls if role == "solver")


def test_assessment_persists_and_edits_require_new_review(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    assert asyncio.run(pipe.preflight(candidate))
    loaded = store.candidates()[0]
    assert has_valid_consistency(loaded, {source.id: source})
    assert asyncio.run(pipe.preflight(loaded)) and len(gateway.calls) == 3
    loaded.item.question += " Explain the graph classification before the integer."
    assert not has_valid_consistency(loaded, {source.id: source})
    assert asyncio.run(pipe.preflight(loaded)) and len(gateway.calls) == 6
    pipe.prompts["consistency_review"] += "\nRevised correctness protocol."
    assert asyncio.run(pipe.preflight(loaded)) and len(gateway.calls) == 9


def test_review_infrastructure_failure_never_becomes_difficulty_or_rejection(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    store.save(candidate)
    original = gateway.call

    async def unavailable(role, prompt, salt, schema=None, **kw):
        if schema is ConsistencyAudit:
            raise ModelResponseError(role, "review-id", "deadline_exceeded")
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = unavailable
    with pytest.raises(ModelResponseError):
        asyncio.run(pipe.sample(candidate))
    saved = store.candidates()[0]
    assert not saved.trials and not saved.consistency and saved.status == "quality_seed"
    assert not any(role == "solver" for role, _, _ in gateway.calls)


def test_correctness_only_command_is_resumable_without_solver(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    submission = Submission(id="review-only", item=candidate.item, solution=candidate.solution)
    result = asyncio.run(pipe.qc([submission], preflight_only=True))
    row = result["candidates"][0]
    assert row["consistency_passed"] and row["samples"] == 0 and row["status"] == "quality_seed"
    assert not row["difficulty_measurement_valid"]
    assert not any(role in ("solver", "judge") for role, _, _ in gateway.calls)
    count = len(gateway.calls)
    asyncio.run(pipe.qc([submission], preflight_only=True))
    assert len(gateway.calls) == count


def test_bad_author_draft_is_repaired_before_difficulty_not_evolved(tmp_path, settings, source, candidate):
    pipe, _, _ = setup_pipeline(tmp_path, settings, source, candidate)

    class Author(FakeGateway):
        async def call(self, role, prompt, salt, schema=None, **kwargs):
            if schema in (Solution, Question):
                self.calls.append((role, prompt, salt))
                value = (candidate.solution if schema is Solution else candidate.item).model_copy(deep=True)
                if ":repair:0:" in salt:
                    if schema is Solution:
                        value.answer = "WRONG_GOLD"
                    else:
                        value.answer = "WRONG_GOLD"
                return value, digest(salt), "author-model"
            if schema is ConsistencyAudit and '"answer": "WRONG_GOLD"' in prompt:
                self.calls.append((role, prompt, salt))
                return defective_review(), digest(salt), "gpt-6-luna"
            return await super().call(role, prompt, salt, schema, **kwargs)

    pipe.gateway = Author(candidate)
    result = asyncio.run(pipe.generate(source, None, {"category": "Math", "subtopic": "组合与图论"}, 0, 0))
    assert result.item.answer == "36" and result.consistency.audit.passed()
    assert result.generation == 0 and result.parent_id is None and result.authoring_complete
    assert not any(role in ("solver", "judge") for role, _, _ in pipe.gateway.calls)
    assert len([c for c in pipe.gateway.calls if c[0] == "consistency"]) == 6


def test_quality_rejected_prefix_stops_lineage_without_four_or_sixteen_samples(tmp_path, settings, source, candidate):
    settings.early_screen_samples = 4
    _, store, _ = setup_pipeline(tmp_path, settings, source, candidate)

    class Defective(PatternGateway):
        async def call(self, role, prompt, salt, schema=None, **kwargs):
            if schema is ConsistencyAudit:
                self.calls.append((role, prompt, salt))
                return defective_review(), digest(salt), "gpt-6-luna"
            return await super().call(role, prompt, salt, schema, **kwargs)

    gateway = Defective(candidate, set(range(16)))
    root = RootPlan(name="broken", source_id=source.id, category="Math", subtopic="组合与图论",
                    focus="A controlled defective gold that must not be interpreted as hard")
    experiment = QuestionExperiment(settings, store, "test", "pilot", tmp_path / "run", gateway,
        plan=ExperimentPlan(roots=[root], max_revisions=2), pricing=pricing())

    async def author(src, parent, target, generation, attempt, brief=None):
        item = candidate.model_copy(deep=True)
        item.id = experiment.root_identity(root, {source.id: source})
        item.root_id = item.id
        store.save(item)
        return item

    experiment.generate = author
    result = asyncio.run(experiment.run_experiment())
    assert result["generated_versions"] == 1 and not result["transitions"]
    assert result["roots"][0]["versions"][0]["decision"] == "quality_rejected"
    assert not any(role in ("solver", "judge") for role, _, _ in gateway.calls)


def test_core_consistency_checks_cannot_be_skipped():
    data = passing_consistency().model_dump()
    data["definitions"]["verdict"] = "not_applicable"
    with pytest.raises(ValidationError, match="cannot be skipped"):
        ConsistencyAudit.model_validate(data)


def test_executable_failure_overrides_model_claim_of_correctness(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        if schema is ConsistencyAudit:
            review = passing_consistency()
            review.substitution_checks[0].rhs = "37"
            return review, digest(salt), "gpt-6-luna"
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    result = asyncio.run(pipe.evaluate(candidate))
    assert result.status == "rejected" and result.consistency.audit.passed()
    assert result.consistency.substitution.verdict == "fail" and not result.trials


def test_unfaithful_translation_cannot_be_a_valid_counterexample(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        if schema is CertificateAudit:
            return CertificateAudit(faithful_translation=False,
                reason="The proposed norm duplicates a constant and must not refute the gold.", checks=[]), digest(salt), "gpt-6-luna"
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    assert not asyncio.run(pipe.preflight(candidate))
    assert any("translation_unverified" in r for r in candidate.reasons) and not candidate.trials


def test_blueprint_failure_stops_question_construction_before_alignment(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    settings.preverify_blueprint = True
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        gateway.calls.append((role,prompt,salt))
        if schema is Solution:
            return candidate.solution.model_copy(deep=True), digest(salt), "author"
        if schema is BlueprintAudit:
            return BlueprintAudit(valid=False,answer_correct=False,derivation_complete=False,source_grounded=True,
                reason="The reference proof has an unsupported essential step."),digest(salt),"gpt-6-luna"
        return await original(role,prompt,salt,schema,**kw)

    gateway.call = call
    with pytest.raises(AuthoringRejected,match="blueprint_failed"):
        asyncio.run(pipe.generate(source,None,{"category":"Math","subtopic":"组合与图论"},0,0))
    assert not any(":align" in salt or role in ("solver","judge") for role,_,salt in gateway.calls)
    assert sum(schema_salt.endswith(":blueprint-audit") for _,_,schema_salt in gateway.calls) == 2


def test_certificate_schema_repair_is_bounded_and_does_not_buy_new_blind_solution(tmp_path,settings,source,candidate):
    pipe,_,gateway=setup_pipeline(tmp_path,settings,source,candidate)
    original=gateway.call
    failed=[]

    async def call(role,prompt,salt,schema=None,**kw):
        if schema is CertificateAudit and not failed:
            failed.append(salt)
            raise ModelResponseError(role,"broken-json","invalid_schema")
        return await original(role,prompt,salt,schema,**kw)

    gateway.call=call
    assert asyncio.run(pipe.preflight(candidate))
    assert candidate.consistency.certificate_request==digest(failed[0]+":schema-repair:1")
    assert sum(salt.endswith(":solve") for _,_,salt in gateway.calls)==1
    assert not any(role=="solver" for role,_,_ in gateway.calls)


def test_theory100_cannot_bypass_required_blueprint_and_design(tmp_path,settings,source,candidate):
    pipe,_,gateway=setup_pipeline(tmp_path,settings,source,candidate)
    settings.require_solution_design=True
    settings.preverify_blueprint=True
    assert not asyncio.run(pipe.preflight(candidate))
    assert "missing_solution_design" in candidate.reasons and "missing_or_failed_blueprint_audit" in candidate.reasons
    assert not gateway.calls


def test_original_physics_counterexample_violates_lossless_power_and_dark_definition():
    # The historical P03B formula, independently falsified at an allegedly admissible point.
    r, kappa, omega = 2, .5j, 1
    b = (1, kappa)
    coefficient = (2j * (1+r) / ((1+kappa*kappa) * (1-r)) / (omega-1j)
                   - 2j*r*(1+r) / ((1+kappa*kappa) * (1-r)) / (omega-1j*r))
    first_column = (1+coefficient, coefficient*kappa)
    assert sum(abs(x)**2 for x in first_column) == pytest.approx(4.2)
    alleged_dark = (kappa, -1)
    residual = tuple(x * sum(alleged_dark) for x in b)
    assert residual != (0, 0)
    assert tuple(x * sum((1, -1)) for x in b) == (0, 0)


def test_blind_derivation_may_disagree_without_automatically_rejecting(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call

    async def call(role, prompt, salt, schema=None, **kw):
        if schema is IndependentSolution:
            return IndependentSolution(outcome="uncertain", derivation=["Attempted theorem use", "Unresolved step"],
                answer="37", issues=["Independent attempt might be wrong"]), digest(salt), "gpt-6-luna"
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    assert asyncio.run(pipe.preflight(candidate))
    assert candidate.consistency.independent_solution.answer == "37"
    assert candidate.consistency.audit.passed()
