"""论证研究合同的门禁、盲审、对照、恢复与4→16；合成答卷不算科学成绩。"""
import asyncio

import pytest
from test_bottleneck import prepare
from test_qc import setup_pipeline

from diamond_factory.argument_study import (
    ArgumentControls,
    ArgumentDecision,
    ArgumentOracle,
    ArgumentStudy,
    ArgumentStudyState,
    ResponseGrade,
    control_errors,
)
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import DeadlineOutcome, Judgment
from diamond_factory.pipeline import QualityRejected, SampleIncomplete


def decision(verdict="yes", count=1):
    return ArgumentDecision(outputs=[{"output_index": i, "verdict": verdict,
        "reasoning": "Synthetic control establishes the requested conclusion and supporting inference.",
        "witness": "The response explicitly supplies the inference required by this synthetic test."} for i in range(count)])


def hle(correct="yes"):
    return Judgment(extracted_final_answer="Synthetic conclusion", reasoning="Controlled final-result match.",
                    correct=correct, confidence=100, strict=True)


def controlled_state():
    controls = ArgumentControls(cases=[{"id": key, "kind": kind, "response": "Synthetic response with distinct payload " + key}
        for key, kind in (("a", "equivalent"), ("b", "equivalent"),
                          ("c", "omitted_argument"), ("d", "invalid_argument"))])
    truth = {"a": "yes", "b": "yes", "c": "no", "d": "no"}
    check = {"verdict": "pass", "reasoning": "The controlled synthetic task is well defined and recoverable.",
                 "witness": "The fixture retains all defining inputs and every required output."}
    oracle = ArgumentOracle(cases=[{"id": k, "verdict": v, "reasoning": "Independent synthetic control truth is stipulated."}
                                   for k, v in truth.items()],
        final_results=[{"id": k, "verdict": "yes", "reasoning": "All final values are retained in this synthetic case."}
                       for k in truth], question_complete=check, target_unambiguous=check,
        golden_valid=check, hidden_steps_recoverable=check)
    grades = {k: ResponseGrade(response="Synthetic response " + k, hle=hle(), argument=decision(v))
              for k, v in truth.items()}
    return ArgumentStudyState(fingerprint="synthetic", controls=controls, oracle=oracle, control_grades=grades)


@pytest.mark.parametrize("arg, match, expected", [("yes", "yes", "yes"), ("no", "yes", "no"),
    ("no", "no", "no"), ("yes", "no", "unresolved"), ("uncertain", "yes", "unresolved")])
def test_proof_validity_and_final_match_are_separate(arg, match, expected):
    grade = ResponseGrade(response="Synthetic response", hle=hle(match), argument=decision(arg))
    assert grade.verdict() == expected
    assert grade.hle.correct == match


@pytest.mark.parametrize("defect", ["mapping", "missing_negative", "wrong_final", "false_accept", "uncertain"])
def test_controls_need_correct_final_results_and_real_argument_negatives(defect):
    state = controlled_state()
    assert not control_errors(state, 1)
    if defect == "mapping":
        state.oracle.cases.pop()
    elif defect == "missing_negative":
        state.controls.cases[-1].kind = "omitted_argument"
    elif defect == "wrong_final":
        state.oracle.final_results[-1].verdict = "no"
    elif defect == "false_accept":
        state.control_grades["d"].argument = decision("yes")
    else:
        state.oracle.golden_valid.verdict = "uncertain"
    assert control_errors(state, 1)


def prepared(tmp_path, settings, source, candidate, monkeypatch):
    prepare(candidate, source)
    settings.samples, settings.early_screen_samples = 16, 4
    settings.require_verifier_adversary = settings.freeze_question_at_verifier = True
    settings.solver_deadline_counts_as_incorrect = True
    candidate.known_result.grading_requirement = "argument_or_witness_validation"
    candidate.authoring_complete = True
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)

    async def preflight(c):
        return True

    monkeypatch.setattr(pipe, "preflight", preflight)
    return pipe, store, gateway


def control_fixture(study):
    def install():
        control = controlled_state()
        study.state.controls, study.state.oracle, study.state.control_grades = (
            control.controls, control.oracle, control.control_grades)
        study._ready = True
    return install


def test_failed_science_preflight_cannot_buy_solver_or_controls(tmp_path, settings, source, candidate, monkeypatch):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)

    async def fail(c):
        return False

    async def forbidden(*args, **kwargs):
        raise AssertionError("No model request is permitted")

    monkeypatch.setattr(pipe, "preflight", fail)
    gateway.call = forbidden
    with pytest.raises(QualityRejected):
        asyncio.run(ArgumentStudy(pipe, candidate).run())


def test_direct_sample_cannot_bypass_science_and_controls(tmp_path, settings, source, candidate, monkeypatch):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No solver call before gates")

    gateway.call = forbidden
    with pytest.raises(QualityRejected):
        asyncio.run(ArgumentStudy(pipe, candidate)._sample(0))


def test_reference_gold_is_not_submitted_to_argument_grading(tmp_path, settings, source, candidate, monkeypatch):
    import json

    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    fixture = controlled_state()
    reviewed = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        if schema is ArgumentControls:
            packet = json.loads(prompt.splitlines()[-1])
            assert set(packet) == {"question", "required_outputs", "reference_derivation"}
            assert packet["reference_derivation"] == candidate.solution.derivation
            return fixture.controls, salt, "fixture-controls"
        if schema is ArgumentOracle:
            packet = json.loads(prompt.splitlines()[-1])
            assert {r["id"] for r in packet["responses"]} == {"a", "b", "c", "d"}
            assert packet["item"]["answer"] == candidate.item.answer
            assert packet["initial_golden"]["solution"]["derivation"]
            return fixture.oracle, salt, "fixture-oracle"
        if schema is ArgumentDecision:
            key = salt.split(":")[-2]
            reviewed.append(key)
            return decision("yes" if key in ("a", "b") else "no"), salt, "fixture-argument"
        return hle(), salt, "fixture-hle"

    gateway.call = call
    asyncio.run(study.controls())
    assert set(reviewed) == set(study.state.control_grades) == {"a", "b", "c", "d"}
    assert not control_errors(study.state, 1)


def test_deadline_has_receipt_and_no_fabricated_answer_or_judge(tmp_path, settings, source, candidate, monkeypatch):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    control_fixture(study)()
    calls = []

    async def timeout(role, *args, **kwargs):
        calls.append(role)
        raise ModelResponseError("solver", "sealed-timeout", "deadline_exceeded")

    gateway.call = timeout
    gateway.deadline_outcome = lambda *args, **kwargs: DeadlineOutcome(sample_id=0,
        solver_request="sealed-timeout", requested_model="glm-5.2", deadline_seconds=600.0, elapsed_seconds=600.1)
    asyncio.run(study._sample(0))
    assert calls == ["solver"] and study.state.samples[0].grade is None
    assert study.state.samples[0].verdict() == "no"
    assert study.report()["actual_answers"] == 0 and study.report()["deadline_incorrect"] == 1


@pytest.mark.parametrize("wrong_first, expected_calls, phase", [(False, 4, "too_easy_first4"),
    (True, 16, "too_easy_16")])
def test_first_four_and_supplement_do_not_change_question_or_production_trials(
        tmp_path, settings, source, candidate, monkeypatch, wrong_first, expected_calls, phase):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    calls = []
    original = candidate.model_dump()

    async def controls():
        control_fixture(study)()

    async def call(role, prompt, salt, *args, **kwargs):
        assert role == "solver" and prompt == candidate.item.question
        calls.append(salt)
        return "Synthetic answer " + salt.rsplit(":", 1)[1], salt, "fixture-solver"

    async def grade(row, identity):
        row.hle = hle()
        row.argument = decision("no" if wrong_first and identity.endswith(":0") else "yes")
        study.save()

    monkeypatch.setattr(study, "controls", controls)
    monkeypatch.setattr(study, "grade", grade)
    gateway.call = call
    report = asyncio.run(study.run())
    assert len(calls) == expected_calls and report["state"] == phase
    assert report["production_deliveries"] == 0
    assert candidate.model_dump() == original and not candidate.trials
    restored = ArgumentStudy(pipe, candidate)
    async def restored_controls():
        control_fixture(restored)()
    monkeypatch.setattr(restored, "controls", restored_controls)
    monkeypatch.setattr(restored, "grade", grade)
    asyncio.run(restored.run())
    assert len(calls) == expected_calls


def test_solver_response_survives_judge_failure_and_changed_input_invalidates_resume(
        tmp_path, settings, source, candidate, monkeypatch):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    control_fixture(study)()
    calls = []

    async def call(role, prompt, salt, *args, **kwargs):
        assert role == "solver"
        calls.append(salt)
        return "Synthetic paid answer", salt, "fixture-solver"

    async def failed(*args):
        raise ModelResponseError("judge", "failed-judge", "http_503")

    gateway.call = call
    monkeypatch.setattr(study, "grade", failed)
    with pytest.raises(ModelResponseError):
        asyncio.run(study._sample(0))
    restored = ArgumentStudy(pipe, candidate)
    control_fixture(restored)()
    assert restored.state.samples[0].grade.response == "Synthetic paid answer"
    assert restored.state.samples[0].verdict() == "pending"

    async def complete(row, identity):
        row.hle, row.argument = hle(), decision()
        restored.save()

    monkeypatch.setattr(restored, "grade", complete)
    asyncio.run(restored._sample(0))
    assert len(calls) == 1 and restored.state.samples[0].verdict() == "yes"
    candidate.item.question += " Changed scientific input."
    with pytest.raises(ValueError, match="inputs/protocol changed"):
        ArgumentStudy(pipe, candidate)


def test_argument_reviewer_is_blind_and_must_map_all_outputs(tmp_path, settings, source, candidate, monkeypatch):
    import json

    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    seen = []

    async def call(role, prompt, salt, schema=None, **kwargs):
        if schema is ArgumentDecision:
            packet = json.loads(prompt.splitlines()[-1])
            assert set(packet) == {"question", "required_outputs", "response"}
            seen.append(packet)
            return decision(count=len(candidate.item.required_outputs) + 1), salt, "fixture-reviewer"
        return hle(), salt, "fixture-hle"

    gateway.call = call
    grade = ResponseGrade(response="Synthetic submitted derivation")
    with pytest.raises(ModelResponseError, match="invalid_argument_output_mapping"):
        asyncio.run(study.grade(grade, "sample"))
    assert seen and grade.argument is None
    assert grade.hle is not None and grade.verdict() == "pending"


def test_uncertain_grade_does_not_trigger_supplement(tmp_path, settings, source, candidate, monkeypatch):
    pipe, _store, gateway = prepared(tmp_path, settings, source, candidate, monkeypatch)
    study = ArgumentStudy(pipe, candidate)
    calls = []

    async def controls():
        control_fixture(study)()

    async def call(role, prompt, salt, *args, **kwargs):
        calls.append(salt)
        return "Synthetic answer", salt, "fixture-solver"

    async def grade(row, identity):
        row.hle, row.argument = hle(), decision("uncertain")
        study.save()

    monkeypatch.setattr(study, "controls", controls)
    monkeypatch.setattr(study, "grade", grade)
    gateway.call = call
    with pytest.raises(SampleIncomplete):
        asyncio.run(study.run())
    assert len(calls) == 4 and study.report()["incorrect"] == 0
    assert study.report()["pending_or_unresolved"] == 4
