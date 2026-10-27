"""依赖合同的绕过、泄露、恢复与失效；合成图例不作为难度成绩。"""
import asyncio
import json

import pytest
from pydantic import ValidationError
from test_bottleneck import prepare, stamp
from test_qc import setup_pipeline

from diamond_factory.config import load
from diamond_factory.dependencies import (
    DependencyBuilder,
    boundary_prompt_hash,
    dependency_blueprint_errors,
    goal_contract_errors,
    valid_dependency,
)
from diamond_factory.dependency_models import (
    DependencyProbe,
    DependencyQuestionReview,
    DependencyTrace,
    PremiseBoundaryAssessment,
    PremiseBoundaryReview,
    PremiseContract,
)
from diamond_factory.difficulty import construction_ready
from diamond_factory.llm import Gateway, ModelResponseError
from diamond_factory.models import ScientificGoal, ScientificGoalAssessment, ScientificGoalSelection, digest
from diamond_factory.pipeline import AuthoringRejected, QualityRejected


def check(verdict="pass"):
    return {"verdict": verdict, "reasoning": "Synthetic dependency check with an explicit premise boundary.",
                "witness": "The public graph definition is retained; the derived edge identity is not supplied."}


def boundary(verdict="pass"):
    return PremiseBoundaryReview(primitive_boundary=check(verdict), dependency_support=check(),
                                exploration_preserved=check())


def question_review():
    return DependencyQuestionReview(claims=[{"claim_id": cid, "exposure": "derived", "check": check()}
        for cid in ("structure", "count")], primitive_sufficiency=check(), target_preserved=check(),
        trace_faithful=check(), probe_validity=check(),
        shortest_correct_route="Use the defining tree structure, then apply the edge-count identity.",
        remaining_reasoning="The identity is a derived conclusion; this synthetic fixture claims no difficult discovery.")


def prepared(tmp_path, source, candidate):
    settings = load("configs/library-dependency-masking.toml")
    prepare(candidate, source)
    candidate.correctness_basis, candidate.source_fidelity = "paper_grounded", "advisory"
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    primitives = [{"id": "p" + str(i), "statement": a, "kind": "definition", "source_locator": "synthetic definition",
        "independence_reason": "This defines the graph model rather than asserting its edge-count consequence."}
        for i, a in enumerate(candidate.solution.assumptions)]
    contract = PremiseContract(primitives=primitives, derived=[
        {"id": "structure", "statement": "The supplied graph has tree structure.", "depends_on": [p["id"] for p in primitives],
             "source_locator": "synthetic definition", "derivation": "Identify connected acyclic graphs with the defined tree class."},
        {"id": "count", "statement": "Its edge count is one less than its vertex count.", "depends_on": ["structure"],
             "source_locator": "synthetic theorem", "derivation": "Apply the tree edge-count theorem under its finite graph premises."}],
        exploration_ids=["structure"], target_dependencies=["count"])
    goal = ScientificGoal(source_id=source.id, target=candidate.known_result.target,
        assumptions=candidate.solution.assumptions, premise_contract=contract,
        reasoning_challenge=["Recover the graph consequences from the independent graph definition."],
        reference_case_ids=[pipe.goal_references.cases[0].id],
        reference_comparison="This fixture tests dependencies only and has no measured reference difficulty.")
    record = ScientificGoalAssessment(goal=goal, goal_sha256=digest(goal.model_dump()), request_id="goal",
        actual_model="fixture", source_sha256=source.sha256,
        references_sha256=digest(pipe.goal_references.model_dump()),
        prompt_sha256=digest(pipe.prompts["scientific_goal"]), model_config_sha256=digest(settings.generator.model_dump()),
        premise_assessment=PremiseBoundaryAssessment(goal_sha256=digest(goal.model_dump()), source_sha256=source.sha256,
            prompt_sha256=boundary_prompt_hash(pipe.prompts), model_config_sha256=digest(settings.consistency.model_dump()),
            review=boundary(), request_id="boundary", actual_model="fixture"))
    candidate.known_result.scientific_goal = record
    candidate.solution.design.dependency_trace = DependencyTrace(retained_primitive_ids=[p["id"] for p in primitives],
        hidden_claim_ids=["structure", "count"], proof_links=[
            {"claim_id": "structure", "proof_step_indices": [1]}, {"claim_id": "count", "proof_step_indices": [2]}])
    candidate.solution.design.difficulty.mask.proof_step_indices = [1, 2]
    stamp(pipe, candidate, source)
    return pipe, store, gateway, goal


def reply(gateway, candidate, review=None, error=None):
    calls = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        data = json.loads(prompt.splitlines()[-1])
        calls.append((schema, data))
        if error:
            raise error
        if schema is DependencyProbe:
            assert data == {"question": candidate.item.question}
            return DependencyProbe(used_givens=["The graph is connected and has no cycles."],
                route=["Identify a tree.", "Apply the edge count identity."], answer="36",
                remaining_reasoning="Only graph recognition and the standard edge-count theorem are needed."), salt, "fixture"
        if schema is DependencyQuestionReview:
            return review or question_review(), salt, "fixture"
        raise AssertionError(schema)

    gateway.call = call
    return calls


def test_contract_forbids_derived_premises_and_disconnected_or_cyclic_graphs(tmp_path, source, candidate):
    _pipe, _store, _gateway, goal = prepared(tmp_path, source, candidate)
    goal.assumptions.append(goal.premise_contract.derived[0].statement)
    assert goal_contract_errors(goal) == ["goal_assumptions_must_be_primitive_inputs"]
    for defect in ("cycle", "unused", "primitive_exploration", "duplicate"):
        payload = goal.premise_contract.model_dump()
        if defect == "cycle":
            payload["derived"][0]["depends_on"] = ["count"]
        elif defect == "unused":
            payload["target_dependencies"] = ["structure"]
        elif defect == "primitive_exploration":
            payload["exploration_ids"] = [payload["primitives"][0]["id"]]
        else:
            payload["derived"][1]["id"] = "structure"
        with pytest.raises(ValidationError):
            PremiseContract.model_validate(payload)


@pytest.mark.parametrize("defect", ["missing", "partial_mask", "missing_link", "extra_premise", "invalid_index"])
def test_blueprint_requires_the_whole_registered_chain(tmp_path, source, candidate, defect):
    _pipe, _store, _gateway, _goal = prepared(tmp_path, source, candidate)
    assert not dependency_blueprint_errors(candidate.known_result, candidate.solution)
    trace = candidate.solution.design.dependency_trace
    if defect == "missing":
        candidate.solution.design.dependency_trace = None
    elif defect == "partial_mask":
        candidate.solution.design.difficulty.mask.proof_step_indices = [2]
    elif defect == "missing_link":
        trace.proof_links.pop(0)
    elif defect == "extra_premise":
        candidate.known_result.assumptions.append("An already established edge-count identity.")
    else:
        trace.proof_links[0].proof_step_indices = [9]
    assert dependency_blueprint_errors(candidate.known_result, candidate.solution)


def test_blind_question_probe_reuse_and_literal_question_change(tmp_path, source, candidate):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    calls = reply(gateway, candidate)
    builder = DependencyBuilder(pipe)
    assert asyncio.run(builder.question(candidate, {source.id: source}))
    assert asyncio.run(builder.question(candidate, {source.id: source}))
    assert len(calls) == 2
    candidate.item.question += " Use the stated vertex count."
    assert not valid_dependency(candidate, {source.id: source}, pipe.settings, pipe.prompts)
    assert asyncio.run(builder.question(candidate, {source.id: source}))
    assert len(calls) == 4


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_upstream_leak_blocks_generic_paid_sampling_and_export(tmp_path, source, candidate, monkeypatch, verdict):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    review = question_review()
    review.claims[0].exposure = "given"
    review.claims[0].check.verdict = verdict
    calls = reply(gateway, candidate, review)

    async def already_reviewed(*args, **kwargs):
        return True

    monkeypatch.setattr(pipe, "review_bottleneck", already_reviewed)
    with pytest.raises(QualityRejected):
        asyncio.run(pipe.sample(candidate, 4))
    assert "dependency_contract_failed:supplied_derived_claim:structure" in candidate.reasons
    assert len(calls) == 2 and not candidate.trials
    assert not construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)
    # 失败收据也可恢复，不为同一泄露重新购买模型检查。
    assert not asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["question", "proof", "boundary", "prompt", "model", "source", "trace", "disabled"])
def test_changed_dependencies_cannot_reuse_approval_or_export(tmp_path, source, candidate, monkeypatch, change):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    reply(gateway, candidate)
    assert asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    monkeypatch.setattr("diamond_factory.verifier.has_valid_verifier", lambda *a, **k: True)
    monkeypatch.setattr("diamond_factory.difficulty.valid_bottleneck", lambda *a, **k: True)
    assert construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)
    if change == "question":
        candidate.item.question += " The edge identity is supplied."
    elif change == "proof":
        candidate.solution.derivation[0] += " Changed dependency argument."
    elif change == "boundary":
        candidate.known_result.scientific_goal.premise_assessment.request_id = ""
    elif change == "prompt":
        pipe.prompts["dependency_question_audit"] += " Changed contract."
    elif change == "model":
        pipe.settings.consistency.model = "changed-model"
    elif change == "source":
        source.sha256 = "changed-source"
    elif change == "trace":
        candidate.solution.design.dependency_trace.hidden_claim_ids = ["count"]
    else:
        pipe.settings.require_dependency_trace = False
    assert not construction_ready(candidate, {source.id: source}, pipe.settings, pipe.prompts)


def test_boundary_error_returns_to_goal_selection_before_lock(tmp_path, source, candidate):
    pipe, _store, gateway, goal = prepared(tmp_path, source, candidate)
    calls = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        calls.append(schema)
        if schema is ScientificGoalSelection:
            return ScientificGoalSelection(outcome="found", goal=goal, reason="Synthetic dependency boundary fixture."), salt, "m"
        if schema is PremiseBoundaryReview:
            return boundary("fail"), salt, "m"
        raise AssertionError("Must not lock an answer after a concrete primitive-boundary failure")

    gateway.call = call
    with pytest.raises(AuthoringRejected, match="premise_boundary_failed"):
        asyncio.run(pipe.select_known_result(source, source.model_dump(),
            {"category": "Math", "subtopic": "组合与图论"}, "root", None))
    assert calls.count(ScientificGoalSelection) == pipe.settings.max_repairs + 1


def test_invalid_review_mapping_and_model_errors_never_become_wrong_answers(tmp_path, source, candidate):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    review = question_review()
    review.claims.pop()
    reply(gateway, candidate, review)
    with pytest.raises(ModelResponseError, match="invalid_dependency_claim_mapping"):
        asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    assert candidate.dependency_assessment is None and not candidate.trials
    reply(gateway, candidate, error=ModelResponseError("consistency", "failed", "invalid_schema"))
    with pytest.raises(ModelResponseError, match="invalid_schema"):
        asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    assert not candidate.trials


def test_ordinary_alternative_and_unresolved_depth_do_not_create_subjective_veto(tmp_path, source, candidate):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    review = question_review()
    review.claims[0].exposure = "unused"
    review.claims[0].check.verdict = "uncertain"
    review.probe_validity.verdict = "fail"
    reply(gateway, candidate, review)
    assert asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    assert candidate.dependency_assessment.review.probe_validity.verdict == "fail"


def test_dependency_mode_requires_the_locked_frozen_goal_protocol():
    settings = load("configs/library-dependency-masking.toml")
    for field in ("scientific_goal_references_path", "use_locked_proof_draft", "freeze_question_at_verifier"):
        payload = settings.model_dump()
        payload[field] = None if field.endswith("path") else False
        with pytest.raises(ValidationError):
            type(settings).model_validate(payload)


def test_generator_structure_repair_is_bounded_and_reuses_paid_receipts(tmp_path, source, candidate, monkeypatch):
    pipe, _store, _gateway, goal = prepared(tmp_path, source, candidate)
    monkeypatch.setenv("NEW_API_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("NEW_API_KEY", "synthetic-fixture-only")
    gateway = Gateway(pipe.settings, tmp_path / "requests")
    pipe.gateway = gateway
    valid = ScientificGoalSelection(outcome="found", goal=goal, reason="Synthetic complete dependency proposal.")
    invalid = valid.model_dump()
    invalid["goal"]["misplaced_design_field"] = "This field must not be present."
    fetches = []

    async def fetch(role, config, payload, request_id, base, key):
        fetches.append(payload)
        body = valid.model_dump_json() if len(fetches) > 1 else json.dumps(invalid)
        return {"response": {"model": config.model}, "actual_model": config.model,
                "content": body, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "usage_unavailable": False}

    monkeypatch.setattr(gateway, "_fetch", fetch)
    first = asyncio.run(pipe.generator_call("Synthetic original goal input.", "goal", ScientificGoalSelection))
    assert first[0] == valid
    assert len(fetches) == 2
    correction = next(json.loads(line) for line in fetches[1]["messages"][1]["content"].splitlines()
                      if line.startswith('{"errors":'))
    assert correction["errors"][0]["location"] == ["goal", "misplaced_design_field"]
    assert "Preserve the scientific proposal" in fetches[1]["messages"][1]["content"]
    # 原错误收据与正确修复都恢复；不得重新购买原调用或原地抹掉失败。
    second = asyncio.run(pipe.generator_call("Synthetic original goal input.", "goal", ScientificGoalSelection))
    assert second == first and len(fetches) == 2
    saved = [json.loads(p.read_text()) for p in gateway.directory.glob("*.json") if p.name != "budget.json"]
    assert len(saved) == 2 and sum(t.get("response_error") == "invalid_schema" for t in saved) == 1
    assert not candidate.trials


@pytest.mark.parametrize("reason, expected", [("invalid_schema", 2), ("http_503", 1), ("deadline_exceeded", 1)])
def test_generator_repair_exhaustion_and_other_failures_are_not_grades(tmp_path, source, candidate, reason, expected):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    calls = []

    async def fail(role, prompt, salt, schema=None, **kwargs):
        calls.append((role, salt))
        raise ModelResponseError(role, "failure", reason)

    gateway.call = fail
    with pytest.raises(ModelResponseError, match=reason):
        asyncio.run(pipe.generator_call("Synthetic input.", "goal", ScientificGoalSelection))
    assert len(calls) == expected and all(role == "generator" for role, _ in calls)
    assert not candidate.trials and not candidate.deadline_outcomes


def test_original_generator_protocol_keeps_no_schema_retry(tmp_path, source, candidate):
    pipe, _store, gateway, _goal = prepared(tmp_path, source, candidate)
    pipe.settings.require_dependency_trace = False
    calls = []

    async def fail(role, prompt, salt, schema=None, **kwargs):
        calls.append(salt)
        raise ModelResponseError(role, "failure", "invalid_schema")

    gateway.call = fail
    with pytest.raises(ModelResponseError, match="invalid_schema"):
        asyncio.run(pipe.generator_call("Synthetic original input.", "goal", ScientificGoalSelection))
    assert calls == ["goal"]
