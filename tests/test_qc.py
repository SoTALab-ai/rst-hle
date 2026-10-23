import asyncio
import json

import pytest
from conftest import passing_consistency, trials
from pydantic import ValidationError

from diamond_factory.gates import (
    can_count,
    correct_count,
    lexical_duplicate,
    production_blockers,
    static_errors,
)
from diamond_factory.models import (
    Audit,
    CertificateAudit,
    ConsistencyAudit,
    DedupVerdict,
    IndependentSolution,
    Judgment,
    Source,
    Submission,
    digest,
)
from diamond_factory.pipeline import Pipeline
from diamond_factory.store import Store


@pytest.mark.parametrize("count,retained", [(0, False), (1, True), (8, True), (9, False), (16, False)])
def test_threshold_boundaries(count, retained):
    n = correct_count(trials(count))
    assert (1 <= n <= 8) is retained


def test_missing_and_reused_trials_fail_closed():
    with pytest.raises(ValueError):
        correct_count(trials(4)[:-1])
    rows = trials(4)
    rows[1].solver_request = rows[0].solver_request
    with pytest.raises(ValueError):
        correct_count(rows)
    rows = trials(4)
    rows[1].sample_id = 0
    with pytest.raises(ValueError):
        correct_count(rows)


def test_gold_cannot_be_unsupported(candidate, source, quotas):
    candidate.item.evidence[0].quote = "This is a fabricated statement not present in the source."
    assert "evidence_quote_not_in_source" in static_errors(candidate, {source.id: source}, quotas)


def test_source_integrity_and_immutability(tmp_path, source):
    with pytest.raises(ValidationError):
        Source(**{**source.model_dump(), "sha256": "bad"})
    store = Store(tmp_path / "s.sqlite")
    store.add_source(source)
    changed = source.model_copy(update={"text": source.text + "Changed", "sha256": ""})
    changed = Source.model_validate(changed.model_dump())
    with pytest.raises(ValueError):
        store.add_source(changed)


def test_unknown_label_and_missing_choices(candidate, source, quotas):
    candidate.item.subtopic = "Not v3"
    candidate.item.answer_type = "multipleChoice"
    errors = static_errors(candidate, {source.id: source}, quotas)
    assert "unknown_v3_category_subtopic" in errors
    assert "missing_multiple_choice_material" in errors


def test_lineage_cannot_fill_independent_quota(candidate, quotas):
    parent = candidate.model_copy(deep=True)
    parent.id = "parent"
    parent.status = "accepted"
    assert "lineage_not_independent" in can_count(candidate, [parent], quotas)


def test_subtopic_quotas_do_not_offset(candidate):
    tiny = {"quotas": [{"category": "Math", "subtopic": "组合与图论", "count": 0},
                       {"category": "Math", "subtopic": "代数", "count": 100}]}
    assert "subtopic_quota_full" in can_count(candidate, [], tiny)


def test_official_contract_and_waiver(settings, quotas):
    assert production_blockers(settings, quotas) == []
    settings.external_dedup_required = True
    assert "train_and_eval_dedup_corpora_required" in production_blockers(settings, quotas)


def test_judge_strict_schema():
    with pytest.raises(ValidationError):
        Judgment(extracted_final_answer="2", reasoning="same", correct="yes", confidence=100, strict=False)


def test_unicode_duplicate():
    assert lexical_duplicate("What is ３ + ２?", "what is 3 + 2")


class FakeGateway:
    def __init__(self, candidate, fail_sample=None, duplicate=False):
        self.candidate = candidate
        self.calls = []
        self.fail_sample = fail_sample
        self.duplicate = duplicate

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        self.calls.append((role, prompt, salt))
        if schema is IndependentSolution:
            return IndependentSolution(outcome="solved", derivation=["The graph is a tree.", "37-1=36."],
                answer="36", issues=[]), digest(salt), "gpt-6-luna"
        if schema is ConsistencyAudit:
            return passing_consistency(), digest(salt), "gpt-6-luna"
        if schema is CertificateAudit:
            data = json.loads(prompt.split("\n")[-1])
            from diamond_factory.models import SubstitutionCase
            return CertificateAudit(faithful_translation=True, reason="All formulas match the literal fixture.",
                checks=[SubstitutionCase.model_validate(c) for c in data["proposed_checks"]]), digest(salt), "gpt-6-luna"
        if role == "solver":
            assert self.candidate.item.rationale not in prompt
            assert "solution" not in salt
            i = int(salt.split(":")[-1])
            if i == self.fail_sample:
                raise RuntimeError("simulated infrastructure error")
            return "Final Answer: " + ("36" if i < 4 else "37"), digest(salt), "glm-5.2"
        if schema is Judgment:
            return Judgment(extracted_final_answer="36" if "Final Answer: 36" in prompt else "37",
                            reasoning="Gold comparison only", correct="yes" if "Final Answer: 36" in prompt else "no",
                            confidence=100, strict=True), digest(salt), "gpt-6-luna"
        if schema is Audit:
            return self.candidate.audit, digest(salt), "gpt-5.6-luna"
        if schema is DedupVerdict:
            data = json.loads(prompt.split("\n")[-1])
            ids = [data["references"][0]["id"]] if self.duplicate else []
            return DedupVerdict(duplicate_ids=ids, reason="semantic comparison"), digest(salt), "gpt-5.6-luna"
        raise AssertionError(role)


def setup_pipeline(tmp_path, settings, source, candidate, fail=None):
    store = Store(tmp_path / "factory.sqlite")
    store.add_source(source)
    gateway = FakeGateway(candidate, fail_sample=fail)
    pipe = Pipeline(settings, store, "test", "pilot", tmp_path / "run", gateway)
    return pipe, store, gateway


def test_complete_qc_blind_samples_and_resume(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    submission = Submission(id="authored", item=candidate.item, solution=candidate.solution)
    result = asyncio.run(pipe.qc([submission]))
    assert result["statuses"] == {"screened": 1}
    assert result["accepted"] == 0
    assert result["candidates"][0]["correct_count"] == 4
    assert len([x for x in gateway.calls if x[0] == "solver"]) == 16
    before = len(gateway.calls)
    asyncio.run(pipe.qc([submission]))
    assert len(gateway.calls) == before
    assert store.runs()["test"]["status"] == "completed"


def test_infrastructure_not_a_wrong_answer(tmp_path, settings, source, candidate):
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate, fail=3)
    with pytest.raises(RuntimeError):
        asyncio.run(pipe.qc([Submission(id="authored", item=candidate.item, solution=candidate.solution)]))
    row = store.candidates()[0]
    assert row.status == "quality_seed"
    assert len(row.trials) == 15
    assert store.runs()["test"]["status"] == "interrupted"


def test_semantic_dedup_all_batches(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    from diamond_factory.models import Record
    pipe.external = [Record(id=str(i), question="Distinct external reference " + str(i)) for i in range(25)]
    assert asyncio.run(pipe.duplicate(candidate)) == []
    assert len([x for x in gateway.calls if ":dedup:" in x[2]]) == 3


def test_changed_config_cannot_resume(tmp_path, settings, source, candidate):
    pipe, store, _ = setup_pipeline(tmp_path, settings, source, candidate)
    pipe.start()
    settings.correct_max = 7
    changed = Pipeline(settings, store, "test", "pilot", tmp_path / "run", FakeGateway(candidate))
    with pytest.raises(ValueError, match="changed"):
        changed.start()
