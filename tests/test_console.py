import hashlib
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import trials

from diamond_factory.console import make_handler, serve
from diamond_factory.console_data import ConsoleData, summarize
from diamond_factory.models import DeadlineOutcome
from diamond_factory.store import Store


@pytest.fixture
def console_data(tmp_path, candidate, source):
    path = tmp_path / "factory.sqlite"
    store = Store(path)
    store.add_source(source)
    store.run("test", {"status": "completed_with_errors", "mode": "pilot", "fingerprint": "frozen",
                       "settings": {"solver": {"model": "glm-5.2", "api_key": "must-not-leak"}},
                       "secret": "must-not-leak"})
    store.save(candidate)
    store.db.close()
    return ConsoleData(str(path), tmp_path / "objects", tmp_path / "reports")


@pytest.fixture
def console_server(console_data):
    with ThreadingHTTPServer(("127.0.0.1", 0), make_handler(console_data)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
        server.shutdown()
        thread.join()


def test_console_readonly_snapshot_and_details(console_data):
    path = console_data.database
    before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    snapshot = console_data.snapshot()
    assert snapshot.read_only and snapshot.backend == "sqlite"
    assert len(snapshot.questions) == 1 and len(snapshot.papers) == 1
    assert snapshot.runs[0].candidates == 1 and snapshot.runs[0].accepted == 0
    assert snapshot.runs[0].http_attempts is None
    detail = console_data.question("candidate1")
    assert detail["candidate"]["item"]["answer"] == "36"
    assert "text" not in detail["sources"][0]
    assert "must-not-leak" not in json.dumps(console_data.run("test"))
    assert "must-not-leak" not in json.dumps(detail["workflow"])
    assert detail["workflow"]["mode"] == "pilot"
    assert detail["workflow"]["events"] == []
    with console_data.connection() as db, pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM candidates")
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before
    assert not console_data.objects.exists()


def test_console_missing_database_does_not_create_it(tmp_path):
    missing = tmp_path / "uncreated" / "factory.sqlite"
    data = ConsoleData(str(missing), tmp_path / "uncreated-objects")
    with pytest.raises(sqlite3.OperationalError):
        data.snapshot()
    assert not missing.parent.exists() and not data.objects.exists()


def test_partial_deadline_and_invalid_samples_are_distinct(candidate, source):
    candidate.trials = trials(2)[:3]
    candidate.deadline_outcomes = [DeadlineOutcome(sample_id=3, solver_request="s3",
        requested_model="glm-5.2", deadline_seconds=600.0, elapsed_seconds=601.0)]
    row = summarize(candidate, {source.id: source})
    assert (row.answered, row.deadlines, row.samples, row.correct) == (3, 1, 4, 2)
    assert not row.difficulty_valid and not row.sample_error
    candidate.trials.append(candidate.trials[0])
    row = summarize(candidate, {source.id: source})
    assert row.sample_error and row.correct is None and not row.difficulty_valid


def test_stored_acceptance_is_not_recreated_from_16_answers(candidate, source):
    candidate.trials = trials(4)
    row = summarize(candidate, {source.id: source})
    assert row.correct == 4 and row.samples == 16
    assert row.status == "quality_seed" and not row.difficulty_valid


def test_archives_are_separate_and_unknown_cost_is_not_zero(console_data):
    console_data.reports.mkdir()
    report = {"run_id": "historical", "status": "failed", "delivery_items": 0,
              "planned_lines": 10, "cost": {"unknown_usage_attempts": 1}}
    (console_data.reports / "result.json").write_text(json.dumps(report))
    (console_data.reports / "metadata.json").write_text(json.dumps({"note": "not a run"}))
    snap = console_data.snapshot()
    assert len(snap.runs) == 1 and len(snap.questions) == 1 and len(snap.archives) == 1
    assert snap.archives[0].known_cost_usd is None and snap.archives[0].unknown_usage == 1
    assert snap.archives[0].planned == 10 and snap.archives[0].formed is None
    assert console_data.archive("historical")["report"] == report
    with pytest.raises(KeyError):
        console_data.archive("../result.json")


def test_object_integrity_and_path_boundary(console_data, tmp_path):
    with pytest.raises(ValueError, match="越界"):
        console_data.object("../outside", "x", 1)
    console_data.objects.mkdir()
    (console_data.objects / "x.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="哈希"):
        console_data.object("x.json", "0" * 64, 2)
    assert console_data.object("x.json", hashlib.sha256(b"{}").hexdigest(), 2) == {}


def test_bad_archive_does_not_hide_valid_reports_or_follow_symlink(console_data, tmp_path):
    console_data.reports.mkdir()
    report = {"run_id": "valid", "cost": {"known_cost_usd": 0.25}}
    (console_data.reports / "valid.json").write_text(json.dumps(report))
    (console_data.reports / "invalid.json").write_text("{")
    (console_data.reports / "bad-schema.json").write_text(json.dumps({
        "run_id": "bad-schema", "cost": {"known_cost_usd": "not-a-number"}}))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"run_id": "private", "cost": {}}))
    (console_data.reports / "symlink.json").symlink_to(outside)
    snap = console_data.snapshot()
    assert [r.id for r in snap.archives] == ["valid"] and len(snap.warnings) == 2
    with pytest.raises(KeyError):
        console_data.archive("private")


def test_http_boundary_no_secrets_and_no_writes(console_server):
    with httpx.Client(base_url=console_server, trust_env=False) as client:
        for path in ("/", "/app.js", "/quality-flow.mjs", "/styles.css", "/katex.min.js", "/favicon.svg", "/api/snapshot",
                     "/api/questions/candidate1", "/api/runs/test"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["Cache-Control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "must-not-leak" not in client.get("/api/runs/test").text
        assert client.post("/api/runs/test", json={"status": "accepted"}).status_code == 501
        assert client.get("/api/questions/unknown").status_code == 404
        assert client.get("/api/archives/unknown").status_code == 404
        assert client.get("/api/snapshot", headers={"Host": "attacker.invalid"}).status_code == 403
        assert client.get("/api/snapshot", headers={"Origin": "https://attacker.invalid"}).status_code == 403
        assert client.get("/%2e%2e/pyproject.toml").status_code == 404
        assert client.get("/.env").status_code == 404


def test_http_data_failure_redacts_connection_details(console_server, console_data):
    console_data.database = "/missing/never-created/db.sqlite"
    response = httpx.get(console_server + "/api/snapshot", trust_env=False)
    assert response.status_code == 503 and "/missing" not in response.text
    assert "error" in response.json()


def test_only_loopback_server_allowed():
    with pytest.raises(ValueError, match="本机"):
        serve("unused", "unused", None, "0.0.0.0", 0)


def test_serve_dispatch_never_opens_writer_or_loads_pipeline_config(monkeypatch, tmp_path):
    from diamond_factory import cli, console

    calls = []
    monkeypatch.setattr("sys.argv", ["diamond", "--config", "does-not-exist.toml", "serve"])
    monkeypatch.delenv("DIAMOND_DATABASE_URL", raising=False)
    monkeypatch.setenv("DIAMOND_OBJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(console, "serve", lambda *args: calls.append(args))
    monkeypatch.setattr(cli, "open_store", lambda *_args, **_kwargs: pytest.fail("打开了生产写者"))
    cli.main()
    assert calls[0][1] == str(tmp_path)
