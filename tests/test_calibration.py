import asyncio

import pytest

from diamond_factory.calibration import BenchmarkQuestion, Calibration, summarize
from diamond_factory.models import Judgment, digest
from diamond_factory.store import Store


def question():
    return BenchmarkQuestion(id="ref1", category="Math", subtopic="组合与图论", question="What is 6 times 6?",
                             answer="36", answer_type="exactMatch", origin="synthetic-test-reference")


def rows(n, complete=True):
    return [{"question_id": "ref1", "sample_id": i, "correct": i < n,
             "solver_request": f"s{i}", "judge_request": f"j{i}"} for i in range(16 if complete else 15)]


@pytest.mark.parametrize("n,band", [(0, "zero_correct"), (1, "in_band"), (8, "in_band"),
                                     (9, "too_easy"), (16, "too_easy")])
def test_calibration_band(n, band):
    result = summarize([question()], rows(n), [])
    assert result["items"][0]["band"] == band
    assert result["delivery_items"] == 0


def test_incomplete_is_not_zero_correct():
    result = summarize([question()], rows(0, False), [{"type": "Timeout"}])
    assert result["items"][0]["band"] == "incomplete"
    assert result["items"][0]["correct_count"] is None


class Gateway:
    def __init__(self):
        self.calls = 0

    async def call(self, role, prompt, salt, schema=None, **kwargs):
        self.calls += 1
        i = int(salt.rsplit(":", 1)[-1])
        if role == "solver":
            assert "answer" not in kwargs["system_prompt"].lower() or "36" not in kwargs["system_prompt"]
            return "36" if i < 4 else "37", digest(salt), "glm-5.2"
        return Judgment(extracted_final_answer="36" if i < 4 else "37", reasoning="Gold comparison",
                        correct="yes" if i < 4 else "no", confidence=100, strict=True), digest(salt), "gpt-6-luna"


def test_reference_run_never_enters_candidate_pool_and_resumes(tmp_path, settings):
    settings.concurrency = 4
    store = Store(tmp_path / "db.sqlite")
    gateway = Gateway()
    run = Calibration(settings, store, "cal-test", tmp_path / "run", gateway)
    result = asyncio.run(run.run([question()]))
    assert result["totals"] == {"in_band": 1}
    assert len(store.candidates()) == 0
    assert gateway.calls == 32
    asyncio.run(run.run([question()]))
    assert gateway.calls == 32


def test_checkpoint_does_not_classify_partial_and_resumes_same_trials(tmp_path, settings):
    store = Store(tmp_path / "db.sqlite")
    gateway = Gateway()
    run = Calibration(settings, store, "checkpoint", tmp_path / "run", gateway)
    result = asyncio.run(run.run([question()], max_new_samples=4))
    assert result["status"] == "checkpointed"
    assert result["items"][0]["band"] == "incomplete"
    assert result["items"][0]["observed_correct"] == 4
    assert result["items"][0]["correct_count"] is None
    assert gateway.calls == 8
    assert asyncio.run(run.run([question()]))["status"] == "completed"
    assert gateway.calls == 32


def test_cancellation_records_interruption(tmp_path, settings):
    class CancelledGateway:
        async def call(self, *args, **kwargs):
            raise asyncio.CancelledError

    store = Store(tmp_path / "db.sqlite")
    run = Calibration(settings, store, "cancel", tmp_path / "run", CancelledGateway())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run.run([question()]))
    assert store.runs()["cancel"]["status"] == "interrupted"


def test_reuse_namespace_retains_independent_samples_and_checks_settings(tmp_path, settings):
    class CachedGateway(Gateway):
        def __init__(self):
            super().__init__()
            self.cache = {}

        async def call(self, role, prompt, salt, schema=None, **kwargs):
            key = (role, prompt, salt)
            if key not in self.cache:
                self.cache[key] = await super().call(role, prompt, salt, schema, **kwargs)
            return self.cache[key]

    store = Store(tmp_path / "db.sqlite")
    gateway = CachedGateway()
    first = Calibration(settings, store, "source", tmp_path / "source", gateway)
    asyncio.run(first.run([question()], max_new_samples=4))
    second = Calibration(settings, store, "subset", tmp_path / "subset", gateway, reuse_run="source")
    assert asyncio.run(second.run([question()]))["totals"] == {"in_band": 1}
    assert gateway.calls == 32
    settings.request_deadline_seconds = 123
    with pytest.raises(ValueError, match="identical"):
        asyncio.run(Calibration(settings, store, "bad", tmp_path / "bad", gateway,
                               reuse_run="source").run([question()]))
