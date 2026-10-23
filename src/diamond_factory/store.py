import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import Candidate, Source


class Store:
    """One writer per database; lock the whole run to prevent quota races."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    @contextmanager
    def lock(self):
        with Path(str(self.path) + ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def add_source(self, source: Source):
        prior = self.db.execute("SELECT body FROM sources WHERE id=?", (source.id,)).fetchone()
        if prior and Source.model_validate_json(prior[0]).sha256 != source.sha256:
            raise ValueError("Source id already exists with different content; use a versioned id")
        self.db.execute("INSERT OR IGNORE INTO sources VALUES (?, ?)", (source.id, source.model_dump_json()))
        self.db.commit()

    def sources(self):
        return [Source.model_validate_json(r[0]) for r in self.db.execute("SELECT body FROM sources ORDER BY id")]

    def save(self, candidate: Candidate):
        self.db.execute("INSERT OR REPLACE INTO candidates VALUES (?, ?)",
                        (candidate.id, candidate.model_dump_json()))
        self.db.commit()

    def candidates(self):
        return [Candidate.model_validate_json(r[0]) for r in self.db.execute(
            "SELECT body FROM candidates ORDER BY rowid")]

    def run(self, run_id: str, body: dict):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)",
                        (run_id, json.dumps(body, ensure_ascii=False)))
        self.db.commit()

    def runs(self):
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT id, body FROM runs")}
