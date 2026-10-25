import asyncio
import json

import pytest
from conftest import passing_consistency
from test_consistency import defective_review
from test_paper_ingest import pg as pg_fixture
from test_qc import FakeGateway, setup_pipeline

from diamond_factory.models import CertificateAudit, Question, Solution, digest
from diamond_factory.preflight_repairs import classify_preflight
from diamond_factory.substitution import verify_substitutions


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("defect,category,owner", [
    ("none", None, None),
    ("review", "scientific_review", "author"),
    ("translation", "certificate_translation", "certificate"),
    ("counterexample", "gold_counterexample", "author"),
    ("execution", "certificate_execution", "certificate"),
    ("coverage", "certificate_execution", "certificate"),
])
def test_structured_attribution(candidate, defect, category, owner):
    review = defective_review() if defect == "review" else passing_consistency()
    certificate = CertificateAudit(faithful_translation=defect != "translation",
        reason="A controlled translation decision for the synthetic fixture.", checks=review.substitution_checks)
    if defect == "counterexample":
        certificate.checks[0].rhs = "37"
    elif defect == "execution":
        certificate.checks[0].lhs = "abs(n-1)"
    elif defect == "coverage":
        certificate.checks = []
    report = verify_substitutions(review.model_copy(update={"substitution_checks": certificate.checks}), candidate.item)
    advice = classify_preflight(review, certificate, report)
    assert (advice.category, advice.owner) == (category, owner) if advice else category is None
    if defect == "counterexample":
        assert "lhs=36" in advice.findings[0] and "rhs=37" in advice.findings[0]
    if defect == "coverage":
        assert advice.uncovered_outputs == candidate.item.required_outputs


def repair_gateway(pipe, candidate, *, always_bad=False, counterexample=False):
    class Repair(FakeGateway):
        certificates = 0

        async def call(self, role, prompt, salt, schema=None, **kwargs):
            if schema is Solution or schema is Question:
                self.calls.append((role, prompt, salt))
                return (candidate.solution if schema is Solution else candidate.item).model_copy(deep=True), digest(salt), "author"
            value, rid, model = await super().call(role, prompt, salt, schema, **kwargs)
            if schema is CertificateAudit:
                self.certificates += 1
                if counterexample:
                    value.checks[0].rhs = "37"
                elif always_bad or self.certificates == 1:
                    value.checks[0].lhs = "abs(n-1)"
            return value, rid, model

    pipe.gateway = Repair(candidate)
    return pipe.gateway


def test_unsupported_certificate_repairs_in_place_preserving_history(tmp_path, settings, source, candidate):
    settings.certificate_max_repairs = 1
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = repair_gateway(pipe, candidate)
    before = digest({"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump()})
    assert asyncio.run(pipe.preflight(candidate))
    assert before == digest({"item": candidate.item.model_dump(), "solution": candidate.solution.model_dump()})
    rows = candidate.consistency.certificate_attempts
    assert [r.substitution.verdict for r in rows] == ["unsupported", "pass"]
    assert rows[0].request_id != rows[1].request_id
    assert store.candidates()[0].consistency.certificate_attempts == rows
    repair_call = next(c for c in gateway.calls if ":certificate-repair:1:" in c[2])
    packet = json.loads(repair_call[1].splitlines()[-1])
    assert packet["repair_feedback"]["classification"]["owner"] == "certificate"
    assert packet["question"] == candidate.item.question and packet["gold_answer"] == candidate.item.answer
    assert sum(salt.endswith(":solve") for _, _, salt in gateway.calls) == 1
    before_calls = len(gateway.calls)
    assert asyncio.run(pipe.preflight(store.candidates()[0]))
    assert len(gateway.calls) == before_calls
    # 收据与最终证书不一致不能获得通过资格。
    candidate.consistency.certificate_attempts[-1].request_id = "tampered"
    assert not candidate.consistency.passed()
    candidate.consistency.certificate_attempts[-1].request_id = candidate.consistency.certificate_request
    candidate.consistency.certificate_attempts[0].substitution.verdict = "fail"
    assert not candidate.consistency.passed()  # 后续通过不能抹去已确认的忠实反例。


def test_faithful_counterexample_never_enters_certificate_repair(tmp_path, settings, source, candidate):
    settings.certificate_max_repairs = 2
    pipe, _, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = repair_gateway(pipe, candidate, counterexample=True)
    assert not asyncio.run(pipe.preflight(candidate))
    assert gateway.certificates == 1
    assert candidate.preflight_repair.owner == "author"
    assert candidate.preflight_repair.category == "gold_counterexample"
    assert not candidate.trials


def test_exhausted_certificate_repair_cannot_trigger_whole_author_rewrite(tmp_path, settings, source, candidate):
    settings.certificate_max_repairs = 1
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = repair_gateway(pipe, candidate, always_bad=True)
    target = {"category": "Math", "subtopic": "组合与图论"}
    result = asyncio.run(pipe.generate(source, None, target, 0, 0))
    assert result.status == "rejected" and result.authoring_complete and not result.trials
    assert result.preflight_repair.owner == "certificate" and gateway.certificates == 2
    assert sum(s.endswith(":solution") for _, _, s in gateway.calls) == 1
    assert sum(s.endswith(":align") for _, _, s in gateway.calls) == 1
    before = len(gateway.calls)
    assert asyncio.run(pipe.generate(source, None, target, 0, 0)) == store.candidates()[0]
    assert len(gateway.calls) == before


def test_repair_policy_change_rechecks_certificate_without_rebuying_blind_solution(tmp_path, settings, source, candidate):
    settings.certificate_max_repairs = 1
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    assert asyncio.run(pipe.preflight(candidate))
    pipe.prompts["consistency_certificate_repair"] += "\nRevised certificate-only repair instruction."
    assert asyncio.run(pipe.preflight(store.candidates()[0]))
    assert sum(salt.endswith(":solve") for _, _, salt in gateway.calls) == 1
    assert sum(salt.endswith(":review") for _, _, salt in gateway.calls) == 1
    assert sum(salt.endswith(":certificate") for _, _, salt in gateway.calls) == 2


def test_cached_unresolved_certificate_restores_failure_owner(tmp_path, settings, source, candidate):
    settings.certificate_max_repairs = 1
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = repair_gateway(pipe, candidate, always_bad=True)
    assert not asyncio.run(pipe.preflight(candidate))
    restored = store.candidates()[0]
    restored.status = "quality_seed"
    restored.preflight_repair = None
    calls = len(gateway.calls)
    assert not asyncio.run(pipe.preflight(restored))
    assert restored.preflight_repair.owner == "certificate" and restored.status == "rejected"
    assert len(gateway.calls) == calls


def test_pg_certificate_repair_history_and_resume(tmp_path, settings, source, candidate, pg):
    settings.certificate_max_repairs = 1
    pipe, _, _ = setup_pipeline(tmp_path, settings, source, candidate)
    gateway = repair_gateway(pipe, candidate)
    pg.add_source(source)
    pipe.store = pg
    pipe.start({"command": "synthetic certificate repair persistence"})
    assert asyncio.run(pipe.preflight(candidate))
    restored = pg.candidates()[0]
    assert restored == candidate and len(restored.consistency.certificate_attempts) == 2
    before = list(gateway.calls)
    assert asyncio.run(pipe.preflight(restored)) and gateway.calls == before
