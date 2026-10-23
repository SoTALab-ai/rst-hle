import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pypdf.errors import PdfReadError
from test_qc import FakeGateway, setup_pipeline

from diamond_factory.artifacts import LocalObjects
from diamond_factory.calibration import BenchmarkQuestion, Calibration, summarize
from diamond_factory.cli import main
from diamond_factory.llm import CacheIntegrityError, Gateway, GatewayConfigurationError, ModelResponseError
from diamond_factory.models import DedupVerdict, Judgment, Question, Solution, Source, Submission, digest
from diamond_factory.pipeline import Pipeline
from diamond_factory.reference_selection import select_examples
from diamond_factory.routing import RoutingDecision, read_routes, route_sources
from diamond_factory.runtime import gather_cancel_on_error
from diamond_factory.sources import download
from diamond_factory.store import Store


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("NEW_API_BASE_URL", "https://testing.invalid/v1")
    monkeypatch.setenv("NEW_API_KEY", "PRIVATE_TEST_KEY")


@dataclass
class ResponseStore:
    objects: LocalObjects
    fail_write: bool = True
    attempts: int = 0

    def __post_init__(self):
        self.requests, self.events = {}, []

    def get_request(self, rid):
        return self.requests.get(rid)

    def reserve_request(self, run, limit):
        if self.attempts >= limit:
            return None
        self.attempts += 1
        return self.attempts

    def save_request(self, run, trace):
        if self.fail_write:
            self.fail_write = False
            raise OSError("Simulated DB outage after the paid response")
        self.requests[trace["request_id"]] = trace

    def event(self, run, body):
        self.events.append(body)

    def save_attempt(self, run, attempt):
        key, sha, size = self.objects.put(attempt)
        self.event(run, {"object_key": key, "object_sha256": sha, "object_size": size})


def test_paid_response_recovers_after_db_write_failure(tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "36"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}})

    mock_http(monkeypatch, handler)
    store = ResponseStore(LocalObjects(tmp_path / "objects"))
    with pytest.raises(OSError):
        asyncio.run(Gateway(settings, tmp_path / "requests", store, "run").call("solver", "Q", "s:0"))
    answer = asyncio.run(Gateway(settings, tmp_path / "requests", store, "run").call("solver", "Q", "s:0"))
    assert answer[0] == "36"
    assert len(calls) == 1 and store.attempts == 1
    receipt = store.events[0]
    assert store.objects.get(receipt["object_key"], receipt["object_sha256"], receipt["object_size"])["usage"] == {
        "completion_tokens": 3}


def test_corrupted_journal_never_triggers_a_new_paid_request(tmp_path, settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "36"}, "finish_reason": "stop"}]})

    mock_http(monkeypatch, handler)
    gateway = Gateway(settings, tmp_path)
    _, rid, _ = asyncio.run(gateway.call("solver", "Q", "s:0"))
    path = tmp_path / (rid + ".json")
    trace = json.loads(path.read_text()); trace["content"] = "tampered"
    path.write_text(json.dumps(trace))
    with pytest.raises(CacheIntegrityError):
        asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "s:0"))
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["http_503", "transport", "malformed", "invalid_schema", "auth"])
def test_all_model_failures_leave_receipts_without_secrets(tmp_path, settings, monkeypatch, kind):
    settings.max_retries = 0
    calls = []

    def handler(request):
        calls.append(request)
        if kind == "transport":
            raise httpx.ConnectError("PRIVATE_TEST_KEY should not enter logs")
        if kind == "auth":
            return httpx.Response(401, text="PRIVATE_TEST_KEY")
        if kind == "http_503":
            return httpx.Response(503, text="PRIVATE_TEST_KEY")
        if kind == "malformed":
            return httpx.Response(200, json={"choices": [], "usage": {"completion_tokens": 99}})
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}})

    mock_http(monkeypatch, handler)
    for _ in range(2):
        with pytest.raises((ModelResponseError, GatewayConfigurationError)):
            asyncio.run(Gateway(settings, tmp_path).call("solver", "Q", "s:0", Judgment if
                                                       kind == "invalid_schema" else None))
    assert len(calls) == 1
    files = list(tmp_path.rglob("*.json"))
    assert "PRIVATE_TEST_KEY" not in "".join(p.read_text() for p in files)
    attempt = json.loads(next((tmp_path / "attempts").rglob("*.json")).read_text())
    if kind == "malformed":
        assert attempt["usage"]["completion_tokens"] == 99
    assert len(list(tmp_path.glob("*.json"))) == 2  # Budget + terminal trace.


def test_fatal_batch_error_cancels_and_drains_siblings():
    flags = []

    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            flags.append("cancelled")

    async def broken():
        await asyncio.sleep(0)
        raise OSError("storage failure")

    with pytest.raises(OSError):
        asyncio.run(gather_cancel_on_error(slow(), broken()))
    assert flags == ["cancelled"]


def test_reference_selection_excludes_previous_questions_and_subtopics():
    blocks = [{"category": "Math", "subtopic": name, "examples": [
        {"question": "A unique synthetic question about " + name, "answer": "x"}]} for name in ["a", "b", "c"]]
    text = "\n".join("```json\n" + json.dumps(b) + "\n```" for b in blocks)
    first, _ = select_examples(text, per_category=1)
    later, provenance = select_examples(text, per_category=2, exclude=first, new_subtopics=True)
    assert len({q.subtopic for q in first + later}) == 3
    assert later == select_examples(text, per_category=2, exclude=first, new_subtopics=True)[0]
    assert provenance["excluded_ids"] == [first[0].id]
    with pytest.raises(ValueError, match="Insufficient"):
        select_examples(text, per_category=3, exclude=first, new_subtopics=True)


def test_summary_cannot_reuse_a_response_across_questions():
    questions = [BenchmarkQuestion(id=str(i), category="Math", subtopic="x", question="Q", answer="A",
                                   answer_type="exactMatch", origin="fixture") for i in range(2)]
    rows = [{"question_id": str(i), "sample_id": 0, "correct": True,
             "solver_request": "same", "judge_request": "j" + str(i)} for i in range(2)]
    with pytest.raises(ValueError, match="across"):
        summarize(questions, rows, [])


def test_fatal_calibration_configuration_error_stops_other_requests(tmp_path, settings):
    class Broken:
        calls = 0
        cancelled = False

        async def call(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0)
                raise GatewayConfigurationError("invalid credentials")
            try:
                await asyncio.sleep(10)
            finally:
                self.cancelled = True

    settings.concurrency = 2
    gateway = Broken()
    store = Store(tmp_path / "db.sqlite")
    q = BenchmarkQuestion(id="q", category="Math", subtopic="x", question="Q", answer="A",
                          answer_type="exactMatch", origin="fixture")
    with pytest.raises(GatewayConfigurationError):
        asyncio.run(Calibration(settings, store, "fatal", tmp_path / "run", gateway).run([q]))
    assert gateway.calls == 2 and gateway.cancelled
    assert store.runs()["fatal"]["status"] == "interrupted"
    assert store.runs()["fatal"]["trial_records"] == []


def test_qc_resumes_after_dedup_failure_without_resampling(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    from diamond_factory.models import Record
    pipe.external = [Record(id="external", question="A completely different external reference and premise")]
    original = gateway.call
    failed = False

    async def call(role, prompt, salt, schema=None, **kw):
        nonlocal failed
        if schema is DedupVerdict and not failed:
            failed = True
            raise ModelResponseError(role, "dedup-id", "http_503")
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    submission = Submission(id="authored", item=candidate.item, solution=candidate.solution)
    with pytest.raises(ModelResponseError):
        asyncio.run(pipe.qc([submission]))
    assert len(store.candidates()[0].trials) == 16
    assert store.candidates()[0].status == "quality_seed"
    result = asyncio.run(pipe.qc([submission]))
    assert result["statuses"] == {"screened": 1}
    assert sum(role == "solver" for role, _, _ in gateway.calls) == 16


def test_sample_failure_does_not_block_next_submission_or_rebuy_successes(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    submissions = [Submission(id=name, item=candidate.item, solution=candidate.solution) for name in ["first", "next"]]
    first_id = digest({"run": "test", "submission": submissions[0].model_dump()})[:24]
    original = gateway.call
    unavailable = True

    async def call(role, prompt, salt, schema=None, **kw):
        if unavailable and role == "solver" and salt == first_id + ":solver:3":
            raise ModelResponseError(role, "unavailable", "deadline_exceeded")
        return await original(role, prompt, salt, schema, **kw)

    gateway.call = call
    result = asyncio.run(pipe.qc(submissions))
    assert result["statuses"] == {"quality_seed": 1, "screened": 1}
    assert store.runs()["test"]["status"] == "completed_with_errors"
    assert len(next(c for c in store.candidates() if c.id == first_id).trials) == 15
    unavailable = False
    before = len([x for x in gateway.calls if x[0] == "solver"])
    asyncio.run(pipe.qc(submissions))
    after = len([x for x in gateway.calls if x[0] == "solver"])
    assert after - before == 1


def test_same_run_rejects_changed_submission_manifest_before_model_calls(tmp_path, settings, source, candidate):
    pipe, _, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    first = Submission(id="first", item=candidate.item, solution=candidate.solution)
    asyncio.run(pipe.qc([first]))
    before = len(gateway.calls)
    changed = first.model_copy(update={"id": "changed"})
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(pipe.qc([changed]))
    assert len(gateway.calls) == before


def test_generated_production_flow_exports_only_its_accepted_run(tmp_path, settings, source, candidate, monkeypatch):
    source = Source(**{**source.model_dump(), "kind": "primary", "title": "Authored theorem fixture"})
    store = Store(tmp_path / "db.sqlite")
    store.add_source(source)

    class Author(FakeGateway):
        async def call(self, role, prompt, salt, schema=None, **kwargs):
            if schema in (Solution, Question):
                self.calls.append((role, prompt, salt))
                return (candidate.solution if schema is Solution else candidate.item), digest(salt), "gpt-5.6-luna"
            return await super().call(role, prompt, salt, schema, **kwargs)

    settings.max_rounds = 2
    settings.source_max_candidates = 2
    route_path = tmp_path / "routes.json"

    class Router:
        async def call(self, *args):
            return RoutingDecision(subtopics=["组合与图论"], reason="The source states a tree graph theorem."), "r", "gpt-5.6-luna"

    asyncio.run(route_sources(settings, store, "source-routing", route_path, Router()))
    settings.source_routes_path = str(route_path)
    gateway = Author(candidate)
    pipe = Pipeline(settings, store, "generated", "production", tmp_path / "run", gateway)
    result = asyncio.run(pipe.run())
    assert result["statuses"] == {"accepted": 1, "rejected": 1}
    before = len(gateway.calls)
    asyncio.run(pipe.run())
    assert len(gateway.calls) == before
    store.run("private-reference", {"mode": "reference_calibration", "status": "completed",
                                     "fingerprint": "fixture", "private_gold": "DO_NOT_EXPORT_REFERENCE_LABELS"})
    monkeypatch.delenv("DIAMOND_DATABASE_URL", raising=False)
    output = tmp_path / "export.jsonl"
    monkeypatch.setattr("sys.argv", ["diamond", "--db", str(store.path), "export", "--output", str(output)])
    main()
    manifest = json.loads(output.with_suffix(".manifest.json").read_text())
    assert manifest["items"] == 1 and set(manifest["runs"]) == {"generated"}
    assert "DO_NOT_EXPORT_REFERENCE_LABELS" not in output.with_suffix(".manifest.json").read_text()


def test_source_router_rejects_stale_library_and_unknown_subtopics(tmp_path, settings, source):
    store = Store(tmp_path / "db.sqlite"); store.add_source(source)

    class Router:
        calls = 0

        async def call(self, *args):
            self.calls += 1
            return RoutingDecision(subtopics=["组合与图论"], reason="The source is about tree graphs."), "r", "fixture"

    router = Router(); output = tmp_path / "routes.json"
    asyncio.run(route_sources(settings, store, "route", output, router))
    asyncio.run(route_sources(settings, store, "route", output, router))
    assert router.calls == 1
    quotas = json.loads(Path(settings.quotas_path).read_text())
    read_routes(output, [source], quotas)
    changed = Source(**{**source.model_dump(), "text": source.text + " New section.", "sha256": ""})
    with pytest.raises(ValueError, match="stale"):
        read_routes(output, [changed], quotas)
    data = json.loads(output.read_text()); data["sources"][source.id]["subtopics"] = ["invented"]
    output.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Unknown"):
        read_routes(output, [source], quotas)


@pytest.mark.parametrize("failure", ["transport", "oversize", "html", "truncated_pdf"])
def test_download_failure_preserves_existing_pdf_and_cleans_partial_files(tmp_path, failure):
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.7\n"
            if failure == "transport":
                raise httpx.ReadError("connection dropped mid-paper")
            yield b"x" * 100

    path = tmp_path / "paper.pdf"; path.write_bytes(b"%PDF-1.7\nexisting")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"<html>login</html>") if failure == "html" else httpx.Response(200, stream=Body()))) as client, \
            pytest.raises((ValueError, httpx.ReadError, PdfReadError)):
        download(client, "https://arxiv.org/pdf/fixture", path, max_bytes=1000 if failure == "truncated_pdf" else 20)
    assert path.read_bytes() == b"%PDF-1.7\nexisting"
    assert list(tmp_path.glob("*.part")) == []


def test_interrupt_keeps_each_completed_pair_and_resume_skips_it(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    original = gateway.call
    four_done = asyncio.Event()
    release = False

    async def call(role, prompt, salt, schema=None, **kw):
        if role == "solver" and int(salt.rsplit(":", 1)[1]) >= 4 and not release:
            await asyncio.Event().wait()
        result = await original(role, prompt, salt, schema, **kw)
        if schema is Judgment and salt.endswith(":3"):
            four_done.set()
        return result

    gateway.call = call
    submission = Submission(id="interrupted", item=candidate.item, solution=candidate.solution)

    async def exercise():
        nonlocal release
        task = asyncio.create_task(pipe.qc([submission]))
        await asyncio.wait_for(four_done.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(store.candidates()[0].trials) == 4
        assert store.runs()["test"]["status"] == "interrupted"
        release = True
        await pipe.qc([submission])

    asyncio.run(exercise())
    solvers = [salt for role, _, salt in gateway.calls if role == "solver"]
    assert len(solvers) == len(set(solvers)) == 16
    assert store.candidates()[0].status == "screened"


def test_routing_is_required_before_automatic_generation(tmp_path, settings, source, candidate):
    pipe, store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    settings.source_routes_path = str(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="route-sources"):
        asyncio.run(pipe.run())
    assert gateway.calls == [] and store.runs() == {}


def test_concurrent_duplicate_logical_request_is_only_bought_once(tmp_path, settings, monkeypatch):
    calls = []
    settings.max_requests = 1

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "36"}, "finish_reason": "stop"}]})

    mock_http(monkeypatch, handler)
    gateway = Gateway(settings, tmp_path)

    async def exercise():
        return await asyncio.gather(*(gateway.call("solver", "Q", "same-logical-sample") for _ in range(3)))

    answers = asyncio.run(exercise())
    assert len(calls) == 1 and len({a[1] for a in answers}) == 1
    assert gateway.attempts == 1


@pytest.mark.parametrize("correct", ["yes", "no"])
def test_judge_schema_repair_keeps_prompt_and_solver_fixed(correct):
    from diamond_factory.evaluation import judge_answer

    class Judge:
        def __init__(self):
            self.calls = []

        async def call(self, role, prompt, salt, schema):
            self.calls.append((role, prompt, salt))
            if len(self.calls) == 1:
                raise ModelResponseError(role, "bad-format", "invalid_schema")
            return Judgment(extracted_final_answer="36", reasoning="Gold comparison", correct=correct,
                            confidence=80, strict=True), "valid-format", "gpt-6-luna"

    gateway = Judge()
    response = asyncio.run(judge_answer(gateway, "EXACT_OFFICIAL_PROMPT_WITH_FIXED_RESPONSE", "sample:0"))
    assert response[0].correct == correct
    assert [role for role, _, _ in gateway.calls] == ["judge", "judge"]
    assert len({prompt for _, prompt, _ in gateway.calls}) == 1
    assert len({salt for _, _, salt in gateway.calls}) == 2


def test_judge_valid_negative_is_never_retried():
    from diamond_factory.evaluation import judge_answer

    class Judge:
        calls = 0

        async def call(self, *args):
            self.calls += 1
            return Judgment(extracted_final_answer="37", reasoning="Does not match", correct="no",
                            confidence=100, strict=True), "r", "gpt-6-luna"

    gateway = Judge()
    assert asyncio.run(judge_answer(gateway, "fixed", "s"))[0].correct == "no"
    assert gateway.calls == 1


def test_judge_format_repairs_have_a_fixed_limit():
    from diamond_factory.evaluation import judge_answer

    class Judge:
        calls = 0

        async def call(self, *args):
            self.calls += 1
            raise ModelResponseError("judge", "bad-format", "invalid_schema")

    gateway = Judge()
    with pytest.raises(ModelResponseError):
        asyncio.run(judge_answer(gateway, "fixed", "s", schema_repairs=1))
    assert gateway.calls == 2
