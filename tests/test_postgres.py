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
    store.db.execute("TRUNCATE diamond.runs, diamond.sources, diamond.jobs, diamond.quotas CASCADE")
    yield store
    store.db.close()


def run_meta(pg, name="test"):
    pg.run(name, {"mode": "pilot", "status": "running", "fingerprint": "test"})


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
