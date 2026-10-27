import os

import psycopg
import pytest
from test_postgres import pg, run_meta

from diamond_factory.console_data import ConsoleData

pytestmark = pytest.mark.skipif(not os.environ.get("DIAMOND_TEST_DATABASE_URL"),
                                reason="Dedicated real PostgreSQL test database not configured")


@pytest.fixture
def console_pg(tmp_path):
    # 复用既有fixture；该fixture在清理前强制验证数据库名称以_test结尾。
    yield from pg.__wrapped__(tmp_path)


def test_console_pg_reads_cas_without_writing(console_pg, candidate, source):
    run_meta(console_pg)
    console_pg.add_source(source)
    console_pg.save(candidate)
    data = ConsoleData(os.environ["DIAMOND_TEST_DATABASE_URL"], console_pg.objects.root)
    snap = data.snapshot()
    assert snap.backend == "postgresql" and snap.read_only
    assert snap.runs[0].http_attempts == 0
    assert len(snap.questions) == 1 and snap.questions[0].id == candidate.id
    assert data.question(candidate.id)["candidate"]["item"]["answer"] == "36"
    assert data.run("test")["fingerprint"] == "test"
    with data.connection() as db:
        assert db.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        assert db.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("DELETE FROM diamond.candidates")
    assert len(console_pg.candidates()) == 1


def test_console_pg_corrupt_cas_refuses_snapshot(console_pg, candidate, source):
    run_meta(console_pg)
    console_pg.add_source(source)
    console_pg.save(candidate)
    row = console_pg.db.execute("SELECT object_key FROM diamond.candidates").fetchone()
    (console_pg.objects.root / row[0]).write_bytes(b"corrupt")
    data = ConsoleData(os.environ["DIAMOND_TEST_DATABASE_URL"], console_pg.objects.root)
    with pytest.raises(ValueError, match="哈希"):
        data.snapshot()
