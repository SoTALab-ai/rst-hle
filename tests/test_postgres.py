import os

import pytest
from conftest import trials
from psycopg.conninfo import conninfo_to_dict

from diamond_factory.postgres import PostgresStore

URL = os.environ.get("DIAMOND_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Dedicated real PostgreSQL test database not configured")


@pytest.fixture
def pg(tmp_path):
    # Test cleanup is deliberately prohibited on any database not named as an isolated test DB.
    assert conninfo_to_dict(URL)["dbname"].endswith("_test")
    store = PostgresStore(URL, tmp_path / "objects", check=False)
    store.migrate()
    store.db.execute("TRUNCATE diamond.runs, diamond.sources, diamond.jobs, diamond.quotas, "
                     "diamond.library_papers, diamond.library_documents CASCADE")
    yield store
    store.db.close()


def run_meta(pg, name="test"):
    pg.run(name, {"mode": "pilot", "status": "running", "fingerprint": "test"})


def test_argument_study_state_is_cas_bound_and_not_a_production_trial(pg, tmp_path, settings, source, candidate, monkeypatch):
    from test_argument_study import control_fixture, prepared

    from diamond_factory.argument_study import ArgumentStudy

    run_meta(pg)
    pg.add_source(source)
    pipe, _offline, _gateway = prepared(tmp_path / "offline", settings, source, candidate, monkeypatch)
    pipe.store = pg
    study = ArgumentStudy(pipe, candidate)
    control_fixture(study)()
    study.save()
    metadata = pg.runs()[study.state_run]
    assert "object_key" in metadata and "state" not in metadata
    restored = ArgumentStudy(pipe, candidate)
    assert restored.state == study.state
    assert pg.db.execute("SELECT count(*) FROM diamond.qc_samples").fetchone()[0] == 0
    (pg.objects.root / metadata["object_key"]).write_text("corrupted study checkpoint")
    with pytest.raises(ValueError, match="corrupt"):
        ArgumentStudy(pipe, candidate)


def test_pg_source_and_candidate_sample_roundtrip(pg, source, candidate):
    run_meta(pg)
    pg.add_source(source)
    candidate.trials = trials(4)
    for t in candidate.trials:
        for role, rid, model in [("solver", t.solver_request, t.solver_actual_model),
                                  ("judge", t.judge_request, t.judge_actual_model)]:
            pg.save_request("test", {"request_id": rid, "role": role, "actual_model": model,
                                     "usage": {"total_tokens": 1}, "content": "test response"})
    pg.save(candidate)
    assert pg.sources()[0] == source
    assert pg.candidates()[0] == candidate
    assert pg.db.execute("SELECT count(*),sum(correct::int) FROM diamond.qc_samples").fetchone() == (16, 4)
    # Tampered objects cannot be used to resume.
    row = pg.db.execute("SELECT object_key FROM diamond.sources LIMIT 1").fetchone()
    (pg.objects.root / row[0]).write_text("corruption")
    with pytest.raises(ValueError, match="corrupt"):
        pg.sources()


def test_pg_scientific_goal_receipt_and_grading_requirement_roundtrip(pg, tmp_path, source, candidate):
    import asyncio

    from test_scientific_goals import author, replies

    from diamond_factory.models import digest

    run_meta(pg)
    pg.add_source(source)
    pipe, _offline, gateway, goal = author(tmp_path / "offline", source, candidate)
    replies(gateway, goal, candidate.known_result)
    result = asyncio.run(pipe.select_known_result(source, source.model_dump(),
        {"category": "Math", "subtopic": "组合与图论"}, "line", None))
    candidate.known_result = result
    candidate.known_result.grading_requirement = "argument_or_witness_validation"
    receipt = result.scientific_goal
    pg.save_request("test", {"request_id": receipt.request_id, "role": "generator",
        "actual_model": receipt.actual_model, "usage": {"total_tokens": 1},
        "content": receipt.goal.model_dump_json()})
    pg.save(candidate)
    stored = pg.candidates()[0]
    assert stored.known_result.scientific_goal == receipt
    assert stored.known_result.grading_requirement == "argument_or_witness_validation"
    assert pg.get_request(receipt.request_id)["content"] == receipt.goal.model_dump_json()
    assert receipt.references_sha256 == digest(pipe.goal_references.model_dump())


def test_pg_full_dependency_contract_and_question_review_roundtrip(pg, tmp_path, source, candidate):
    import asyncio

    from test_dependencies import prepared, reply

    from diamond_factory.dependencies import DependencyBuilder, valid_dependency

    run_meta(pg)
    pg.add_source(source)
    pipe, _offline, gateway, _goal = prepared(tmp_path / "offline", source, candidate)
    reply(gateway, candidate)
    assert asyncio.run(DependencyBuilder(pipe).question(candidate, {source.id: source}))
    record, assessment = candidate.known_result.scientific_goal, candidate.dependency_assessment
    receipts = [(record.request_id, "generator"), (record.premise_assessment.request_id, "consistency"),
                (assessment.probe_request, "consistency"), (assessment.review_request, "consistency")]
    for rid, role in receipts:
        pg.save_request("test", {"request_id": rid, "role": role, "actual_model": "fixture",
                                "usage": {"total_tokens": 1}, "content": "synthetic dependency receipt"})
    pg.save(candidate)
    stored = pg.candidates()[0]
    # 旧蓝图字段按基类反序列化；比较持久化内容及本需求的具体类型记录。
    assert stored.model_dump() == candidate.model_dump()
    assert stored.known_result.scientific_goal == record
    assert stored.solution.design.dependency_trace == candidate.solution.design.dependency_trace
    assert stored.dependency_assessment == assessment
    assert valid_dependency(stored, {source.id: source}, pipe.settings, pipe.prompts)
    stored.item.question += " Now supply an intermediate that was hidden."
    assert not valid_dependency(stored, {source.id: source}, pipe.settings, pipe.prompts)


@pytest.mark.parametrize("shortcut", [False, True])
def test_pg_difficulty_checkpoints_and_two_stage_history_roundtrip(pg, tmp_path, source, candidate, shortcut):
    import asyncio

    from test_bottleneck import prepare
    from test_difficulty_adversary import DepthGateway

    from diamond_factory.config import load
    from diamond_factory.difficulty import construction_ready
    from diamond_factory.pipeline import Pipeline

    prepare(candidate, source)
    pg.add_source(source)
    from test_shortcut_adversary import ShortcutGateway

    settings = load("configs/library-shortcut-adversary.toml" if shortcut else "configs/library-difficulty-adversary.toml")
    gateway = (ShortcutGateway if shortcut else DepthGateway)(candidate, design=["too_easy", "sufficient"], question=["too_easy", "sufficient"])
    pipe = Pipeline(settings, pg, "depth-test", "pilot", tmp_path / "run", gateway)
    target = {"category": "Math", "subtopic": "组合与图论"}
    result = asyncio.run(pipe.generate(source, None, target, 0, 0))
    stored = pg.candidates()[0]
    # StepwiseBlueprintAudit回读为声明的BlueprintAudit基类；比较完整持久化值，不比较Python子类身份。
    assert stored.model_dump() == result.model_dump() and len(stored.difficulty_rounds) == 2
    assert pg.runs()["depth-test"]["difficulty_checkpoints"]
    resumed = Pipeline(settings, pg, "depth-test", "pilot", tmp_path / "run", gateway)
    count = len(gateway.calls)
    assert asyncio.run(resumed.preflight(stored)) and len(gateway.calls) == count
    assert construction_ready(stored, {source.id: source}, settings, pipe.prompts)
    # run元数据的轻量指针确实从CAS恢复；破坏对象后不得重购提案。
    pointer = next(iter(resumed.difficulty_checkpoints.values()))
    (pg.objects.root / pointer["key"]).write_text("corrupt")
    from diamond_factory.difficulty_adversary import DifficultyAdversary
    with pytest.raises(ValueError, match="corrupt"):
        asyncio.run(DifficultyAdversary(resumed).design(source, target, result.id, None))


def test_pg_advisory_provenance_is_persisted_without_relabelling_sources(pg, source, candidate):
    run_meta(pg)
    pg.add_source(source)
    candidate.source_fidelity = 'advisory'
    candidate.source_notes = ['source_inspired_construction', 'evidence_quote_not_in_source']
    pg.save(candidate)
    loaded = pg.candidates()[0]
    assert loaded.source_fidelity == 'advisory' and loaded.source_notes == candidate.source_notes
    assert pg.sources()[0] == source


def test_pg_preflight_certificate_roundtrip_and_content_binding(pg, tmp_path, settings, source, candidate):
    import asyncio

    from test_qc import FakeGateway

    from diamond_factory.consistency import has_valid_consistency
    from diamond_factory.pipeline import Pipeline

    run_meta(pg)
    pg.add_source(source)
    pipe = Pipeline(settings, pg, "test", "pilot", tmp_path / "run", FakeGateway(candidate))
    assert asyncio.run(pipe.preflight(candidate))
    loaded = pg.candidates()[0]
    assert loaded.consistency == candidate.consistency and loaded.consistency.substitution.verdict == "pass"
    assert has_valid_consistency(loaded, {source.id: source})
    loaded.item.answer = "37"
    assert not has_valid_consistency(loaded, {source.id: source})


def test_pg_budget_atomic_between_connections(pg):
    run_meta(pg)
    other = PostgresStore(URL, pg.objects.root)
    assert pg.reserve_request("test", 1) == 1
    assert other.reserve_request("test", 1) is None
    other.db.close()


def test_pg_lease_expiry_and_stale_worker_fencing(pg):
    pg.enqueue("a", "isolated-test", {"step": "qc"})
    pg.enqueue("b", "other-queue", {})
    first = pg.claim("isolated-test", "worker-1", seconds=0)
    second = pg.claim("isolated-test", "worker-2")
    assert first[0] == second[0] == "a"
    assert first[2] != second[2]
    with pytest.raises(ValueError, match="fenced"):
        pg.finish(first[0], first[2], True)
    pg.finish(second[0], second[2], True)
    assert pg.claim("isolated-test", "worker-3") is None


def test_pg_writer_lock_cross_process(pg):
    other = PostgresStore(URL, pg.objects.root)
    with pg.lock(), pytest.raises(RuntimeError, match="writer"), other.lock():
        pass
    with other.lock():
        pass
    other.db.close()


def test_pg_run_checkpoint_does_not_reset_request_budget(pg):
    run_meta(pg)
    assert pg.reserve_request("test", 2) == 1
    pg.run("test", {"mode": "pilot", "status": "checkpointed", "fingerprint": "test"})
    assert pg.reserve_request("test", 2) == 2
    assert pg.reserve_request("test", 2) is None


def test_pg_catalog_upgrade_preserves_existing_sources_and_run_budget(pg, source):
    pg.add_source(source)
    run_meta(pg)
    pg.reserve_request("test",10)
    pg.db.execute("DROP TABLE diamond.paper_catalog,diamond.paper_metadata_versions")
    pg.db.execute("DELETE FROM diamond.schema_migrations WHERE version=2")
    pg.migrate();pg.migrate()
    assert pg.sources()[0].sha256==source.sha256
    assert pg.db.execute("SELECT http_attempts FROM diamond.runs WHERE id='test'").fetchone()[0]==1
    assert pg.db.execute("SELECT count(*) FROM diamond.schema_migrations").fetchone()[0]==5


def test_pg_paper_metadata_history_and_domain_query(pg):
    from diamond_factory.catalog import Paper, save_catalog
    from diamond_factory.models import Source
    p=Paper(source_id="arxiv:1304.4132v2",arxiv_id="1304.4132v2",title="Synthetic catalog fixture",
        authors=["Test author"],domain="Math",subtopic="组合与图论",journal="Test journal",
        publication_year=2020,doi="10.1000/test",publisher_url="https://example.org/paper",
        venue_evidence_url="https://example.org/metadata",arxiv_categories=["math.CO"],
        selection_reason="A controlled fixture, not a real publication claim.").sealed()
    pg.add_source(Source(id=p.source_id,title=p.title,uri="synthetic://test",kind="synthetic",text="fixture "*40))
    save_catalog(pg,[p]);save_catalog(pg,[p])
    revised=p.model_copy(update={"journal":"Corrected test journal","metadata_sha256":""}).sealed()
    save_catalog(pg,[revised])
    assert pg.papers("Math")[0]["journal"]=="Corrected test journal" and pg.papers("Physics")==[]
    assert pg.db.execute("SELECT count(*) FROM diamond.paper_metadata_versions").fetchone()[0]==2


def test_pg_duplicate_accepted_lineage_rolls_back_without_partial_samples(pg, source, candidate):
    import psycopg
    run_meta(pg)
    pg.add_source(source)
    candidate.status = "accepted"
    pg.save(candidate)
    duplicate = candidate.model_copy(deep=True)
    duplicate.id = "duplicate-root"
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.save(duplicate)
    assert len(pg.candidates()) == 1
    assert pg.db.execute("SELECT count(*) FROM diamond.candidate_sources").fetchone()[0] == 1


@pytest.mark.parametrize("fault_phase", ["save_request", "save_attempt"])
def test_pg_paid_response_journal_recovers_missing_db_index(pg, tmp_path, settings, monkeypatch, fault_phase):
    import asyncio

    import httpx
    from test_gateway import install_mock

    from diamond_factory.llm import Gateway

    run_meta(pg)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "36"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}})

    install_mock(monkeypatch, handler)
    original = getattr(pg, fault_phase)
    failed = False

    def write(run, trace):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Injected failure after object journal persistence")
        original(run, trace)

    monkeypatch.setattr(pg, fault_phase, write)
    directory = tmp_path / "requests"
    with pytest.raises(OSError):
        asyncio.run(Gateway(settings, directory, pg, "test").call("solver", "Q", "sample:0"))
    assert asyncio.run(Gateway(settings, directory, pg, "test").call("solver", "Q", "sample:0"))[0] == "36"
    assert len(calls) == 1
    assert pg.db.execute("SELECT http_attempts FROM diamond.runs WHERE id='test'").fetchone()[0] == 1
    assert pg.db.execute("SELECT count(*) FROM diamond.model_requests").fetchone()[0] == 1
    assert pg.db.execute("SELECT count(*) FROM diamond.stage_events WHERE event='model_http_attempt'").fetchone()[0] == 1


def test_pg_cross_run_cache_reuse_keeps_attempts_with_original_run(pg, tmp_path, settings, monkeypatch):
    import asyncio

    import httpx
    from test_gateway import install_mock

    from diamond_factory.llm import Gateway

    run_meta(pg, "source-run"); run_meta(pg, "subset-run")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "glm-5.2", "choices": [
            {"message": {"content": "36"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}})

    install_mock(monkeypatch, handler)
    first = asyncio.run(Gateway(settings, tmp_path / "first", pg, "source-run").call("solver", "Q", "namespace:0"))
    second = asyncio.run(Gateway(settings, tmp_path / "second", pg, "subset-run").call("solver", "Q", "namespace:0"))
    assert first == second and len(calls) == 1
    assert pg.db.execute("SELECT http_attempts FROM diamond.runs WHERE id='subset-run'").fetchone()[0] == 0
    assert pg.db.execute("SELECT count(*) FROM diamond.stage_events WHERE event='model_http_attempt'").fetchone()[0] == 1
    assert pg.db.execute("SELECT run_id FROM diamond.stage_events WHERE event='model_http_attempt'").fetchone()[0] == "source-run"


def test_pg_unlimited_request_counter_still_persists_and_finite_cap_remains_atomic(pg):
    run_meta(pg)
    other=PostgresStore(URL,pg.objects.root)
    assert pg.reserve_request('test',None)==1
    assert other.reserve_request('test',None)==2
    assert pg.reserve_request('test',1) is None
    assert other.reserve_request('test',None)==3
    assert pg.db.execute("SELECT http_attempts FROM diamond.runs WHERE id='test'").fetchone()[0]==3
    other.db.close()


def test_pg_deadline_outcome_has_own_table_and_no_fabricated_judge(pg, source, candidate):
    from diamond_factory.models import DeadlineOutcome
    run_meta(pg)
    pg.add_source(source)
    pg.save_request('test', {'request_id':'expired','role':'solver','actual_model':'','usage':{},
                            'content':None,'response_error':'deadline_exceeded'})
    candidate.deadline_outcomes=[DeadlineOutcome(sample_id=0,solver_request='expired',
        requested_model='glm-5.2',deadline_seconds=600.0,elapsed_seconds=600.01)]
    pg.save(candidate)
    assert pg.candidates()[0].deadline_outcomes==candidate.deadline_outcomes
    assert pg.db.execute('SELECT count(*) FROM diamond.qc_samples').fetchone()[0]==0
    assert pg.db.execute('SELECT requested_model,reason FROM diamond.qc_deadlines').fetchone()==(
        'glm-5.2','deadline_exceeded')
    candidate.trials=trials(0)[:1]
    with pytest.raises(ValueError,match='cannot also'):
        pg.save(candidate)
    assert not pg.candidates()[0].trials
