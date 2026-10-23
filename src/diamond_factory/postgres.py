import hashlib
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from .artifacts import LocalObjects
from .models import Candidate, Source, digest


class PostgresStore:
    """Database is the state authority; large immutable bodies live in a separate artifact repository."""

    def __init__(self, url, objects_dir="work/objects", check=True):
        self.db = psycopg.connect(url, autocommit=True, application_name="rst-hle:" + str(os.getpid()),
                                  connect_timeout=10)
        self.objects = LocalObjects(objects_dir)
        self.db.execute("SET lock_timeout = '5s'")
        self.db.execute("SET statement_timeout = '30s'")
        self.migration = Path(__file__).parent / "sql/001_initial.sql"
        self.checksum = hashlib.sha256(self.migration.read_bytes()).hexdigest()
        if check:
            self.check_schema()

    def migrate(self):
        with self.db.transaction():
            self.db.execute("SELECT pg_advisory_xact_lock(704693171)")
            exists = self.db.execute("SELECT to_regclass('diamond.schema_migrations')").fetchone()[0]
            if not exists:
                self.db.execute(self.migration.read_text())
                self.db.execute("INSERT INTO diamond.schema_migrations(version,checksum) VALUES(1,%s)",
                                (self.checksum,))
            self.check_schema()

    def check_schema(self):
        if not self.db.execute("SELECT to_regclass('diamond.schema_migrations')").fetchone()[0]:
            raise ValueError("Database not initialized; run 'diamond db-init' explicitly")
        row = self.db.execute("SELECT checksum FROM diamond.schema_migrations WHERE version=1").fetchone()
        if not row or row[0] != self.checksum:
            raise ValueError("Schema migration checksum mismatch")

    @contextmanager
    def lock(self):
        if not self.db.execute("SELECT pg_try_advisory_lock(704693172)").fetchone()[0]:
            raise RuntimeError("Another pipeline writer owns this database; retry later")
        try:
            yield
        finally:
            self.db.execute("SELECT pg_advisory_unlock(704693172)")

    def add_source(self, source):
        key, sha, size = self.objects.put(source.model_dump())
        with self.db.transaction():
            prior = self.db.execute("SELECT text_sha256 FROM diamond.sources WHERE id=%s FOR UPDATE",
                                    (source.id,)).fetchone()
            if prior and prior[0] != source.sha256:
                raise ValueError("Source identity changed; use a versioned id")
            self.db.execute("""INSERT INTO diamond.sources
                (id,title,uri,kind,text_sha256,object_key,object_sha256,object_size)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                            (source.id, source.title, source.uri, source.kind, source.sha256, key, sha, size))

    def sources(self):
        return [Source.model_validate(self.objects.get(*r)) for r in self.db.execute(
            "SELECT object_key,object_sha256,object_size FROM diamond.sources ORDER BY id")]

    def run(self, run_id, body):
        self.db.execute("""INSERT INTO diamond.runs(id,mode,status,fingerprint,body) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,status=excluded.status,
            fingerprint=excluded.fingerprint,body=excluded.body,updated_at=now()""",
                        (run_id, body["mode"], body["status"], body["fingerprint"], Jsonb(body)))

    def runs(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT id,body FROM diamond.runs ORDER BY created_at")}

    def save(self, candidate):
        key, sha, size = self.objects.put(candidate.model_dump())
        with self.db.transaction():
            self.db.execute("""INSERT INTO diamond.candidates
                (id,run_id,parent_id,root_id,generation,category,subtopic,material_key,status,
                 object_key,object_sha256,object_size) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,object_key=excluded.object_key,
                object_sha256=excluded.object_sha256,object_size=excluded.object_size,updated_at=now()""",
                            (candidate.id, candidate.run_id, candidate.parent_id, candidate.root_id,
                             candidate.generation, candidate.item.category, candidate.item.subtopic,
                             candidate.item.material_key, candidate.status, key, sha, size))
            for source_id in candidate.source_ids:
                self.db.execute("INSERT INTO diamond.candidate_sources VALUES(%s,%s) ON CONFLICT DO NOTHING",
                                (candidate.id, source_id))
            for trial in candidate.trials:
                self.db.execute("""INSERT INTO diamond.qc_samples VALUES(%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(candidate_id,sample_id) DO UPDATE SET solver_request=excluded.solver_request,
                    judge_request=excluded.judge_request,correct=excluded.correct,
                    extracted_answer=excluded.extracted_answer""",
                                (candidate.id, trial.sample_id, trial.solver_request, trial.judge_request,
                                 trial.judgment.correct == "yes", trial.judgment.extracted_final_answer))

    def candidates(self):
        return [Candidate.model_validate(self.objects.get(*r)) for r in self.db.execute(
            "SELECT object_key,object_sha256,object_size FROM diamond.candidates ORDER BY created_at,id")]

    def event(self, run_id, data):
        self.db.execute("INSERT INTO diamond.stage_events(run_id,event,data) VALUES(%s,%s,%s)",
                        (run_id, data["event"], Jsonb(data)))

    def get_request(self, request_id):
        row = self.db.execute("SELECT object_key,object_sha256,object_size FROM diamond.model_requests WHERE id=%s",
                              (request_id,)).fetchone()
        return self.objects.get(*row) if row else None

    def save_request(self, run_id, trace):
        key, sha, size = self.objects.put(trace)
        self.db.execute("""INSERT INTO diamond.model_requests
            (id,run_id,role,actual_model,usage,object_key,object_sha256,object_size)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                        (trace["request_id"], run_id, trace["role"], trace["actual_model"],
                         Jsonb(trace["usage"]), key, sha, size))

    def save_attempt(self, run_id, attempt):
        key, sha, size = self.objects.put(attempt)
        receipt = {"event": "model_http_attempt", "request_id": attempt["request_id"],
                   "ordinal": attempt["ordinal"], "object_key": key, "object_sha256": sha, "object_size": size}
        # CLI holds the database writer lock; replaying a recovered journal is idempotent.
        self.db.execute("""INSERT INTO diamond.stage_events(run_id,event,data)
            SELECT %s,'model_http_attempt',%s WHERE NOT EXISTS (
                SELECT 1 FROM diamond.stage_events WHERE run_id=%s AND event='model_http_attempt'
                AND data->>'request_id'=%s AND (data->>'ordinal')::int=%s)""",
                        (run_id, Jsonb(receipt), run_id, attempt["request_id"], attempt["ordinal"]))

    def reserve_request(self, run_id, limit):
        row = self.db.execute("""UPDATE diamond.runs SET http_attempts=http_attempts+1
            WHERE id=%s AND http_attempts < %s RETURNING http_attempts""", (run_id, limit)).fetchone()
        return row[0] if row else None

    def save_quotas(self, quotas):
        manifest = digest(quotas)
        with self.db.transaction():
            for q in quotas["quotas"]:
                self.db.execute("INSERT INTO diamond.quotas VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                                (manifest, q["category"], q["subtopic"], q["count"]))

    def export(self, manifest):
        self.db.execute("INSERT INTO diamond.exports VALUES(%s,%s) ON CONFLICT DO NOTHING",
                        (digest(manifest), Jsonb(manifest)))

    def enqueue(self, job_id, queue, payload):
        self.db.execute("INSERT INTO diamond.jobs(id,queue,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                        (job_id, queue, Jsonb(payload)))

    def claim(self, queue, worker, seconds=120):
        token = str(uuid.uuid4())
        with self.db.transaction():
            row = self.db.execute("""SELECT id FROM diamond.jobs WHERE queue=%s AND
                (status='queued' OR (status='running' AND lease_until < now()))
                ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""", (queue,)).fetchone()
            if not row:
                return None
            return self.db.execute("""UPDATE diamond.jobs SET status='running',lease_owner=%s,lease_token=%s,
                lease_until=now()+(%s * interval '1 second'),attempts=attempts+1,updated_at=now()
                WHERE id=%s RETURNING id,payload,lease_token""", (worker, token, seconds, row[0])).fetchone()

    def finish(self, job_id, token, success):
        row = self.db.execute("""UPDATE diamond.jobs SET status=%s,lease_until=NULL,updated_at=now()
            WHERE id=%s AND lease_token=%s AND status='running' AND lease_until > now() RETURNING id""",
                              ("succeeded" if success else "failed", job_id, token)).fetchone()
        if not row:
            raise ValueError("Lease expired or fenced by another worker")


def open_store(path, *, initialize=False):
    url = os.environ.get("DIAMOND_DATABASE_URL")
    if url:
        return PostgresStore(url, os.environ.get("DIAMOND_OBJECTS_DIR", "work/objects"), check=not initialize)
    if initialize:
        raise ValueError("db-init requires DIAMOND_DATABASE_URL")
    from .store import Store
    return Store(path)
