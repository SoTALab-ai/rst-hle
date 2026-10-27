"""控制台只读投影；不初始化数据库、不取得生产写锁、不重新判题。"""
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psycopg
from pydantic import Field

from .consistency import has_valid_consistency
from .gates import correct_count, sample_outcomes
from .models import Candidate, Source, StrictModel
from .verifier import has_valid_verifier


class QuestionRow(StrictModel):
    id: str
    run_id: str
    root_id: str
    parent_id: str | None
    generation: int
    category: str
    subtopic: str
    question: str
    status: str
    reasons: list[str]
    answered: int
    deadlines: int
    correct: int | None
    samples: int
    sample_error: bool
    consistency_passed: bool
    verifier_passed: bool
    difficulty_valid: bool


class RunRow(StrictModel):
    id: str
    status: str
    mode: str
    fingerprint: str
    updated_at: str | None = None
    candidates: int
    accepted: int
    http_attempts: int | None = None


class PaperRow(StrictModel):
    id: str
    title: str
    domain: str
    journal: str
    doi: str | None = None
    uri: str | None = None
    sha256: str | None = None
    kind: str
    documents: int | None = None
    pages: int | None = None


class ArchiveRow(StrictModel):
    id: str
    filename: str
    status: str
    finalized_at: str | None = None
    planned: int | None = None
    formed: int | None = None
    retained: int | None = None
    known_cost_usd: float | None = None
    http_attempts: int | None = None
    unknown_usage: int | None = None
    unsettled: int | None = None
    by_role: dict = Field(default_factory=dict)
    pricing_date: str | None = None


class Snapshot(StrictModel):
    generated_at: str
    backend: Literal["sqlite", "postgresql"]
    read_only: Literal[True] = True
    runs: list[RunRow]
    questions: list[QuestionRow]
    papers: list[PaperRow]
    archives: list[ArchiveRow]
    statuses: dict[str, int]
    warnings: list[str]


def summarize(candidate: Candidate, sources: dict[str, Source]) -> QuestionRow:
    outcomes = sample_outcomes(candidate)
    correct = None
    sample_error = False
    if outcomes:
        try:
            correct = correct_count(outcomes, samples=len(outcomes))
        except ValueError:
            sample_error = True
    consistency = has_valid_consistency(candidate, sources)
    verifier = bool(candidate.source_ids and set(candidate.source_ids) <= sources.keys()
                    and has_valid_verifier(candidate, sources))
    return QuestionRow(
        id=candidate.id, run_id=candidate.run_id, root_id=candidate.root_id,
        parent_id=candidate.parent_id, generation=candidate.generation,
        category=candidate.item.category, subtopic=candidate.item.subtopic,
        question=candidate.item.question, status=candidate.status, reasons=candidate.reasons,
        answered=len(candidate.trials), deadlines=len(candidate.deadline_outcomes),
        correct=correct, samples=len(outcomes), sample_error=sample_error,
        consistency_passed=consistency, verifier_passed=verifier,
        difficulty_valid=(candidate.audit.passed() and consistency and verifier
                          and len(outcomes) == 16 and not sample_error),
    )


class ConsoleData:
    def __init__(self, database: str, objects: str | Path, reports: str | Path | None = None):
        self.database = database
        self.objects = Path(objects).resolve()
        self.reports = Path(reports).resolve() if reports else None
        self.is_pg = database.startswith(("postgresql://", "postgres://"))

    @contextmanager
    def connection(self):
        if self.is_pg:
            db = psycopg.connect(self.database, autocommit=True, connect_timeout=5,
                                application_name="rst-hle-console-readonly")
            try:
                db.execute("SET default_transaction_read_only = on")
                db.execute("SET statement_timeout = '10s'")
                # 一次响应中的批次与候选来自相同数据库快照。
                with db.transaction():
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    yield db
            finally:
                db.close()
        else:
            uri = Path(self.database).resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                yield db
            finally:
                db.close()

    def object(self, key: str, sha: str, size: int):
        path = (self.objects / key).resolve()
        if not path.is_relative_to(self.objects):
            raise ValueError("对象路径越界")
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("对象缺失或哈希不一致")
        return json.loads(gzip.decompress(data) if key.endswith(".gz") else data)

    def sources(self, db):
        if self.is_pg:
            rows = db.execute("SELECT object_key,object_sha256,object_size FROM diamond.sources")
            return [Source.model_validate(self.object(*r)) for r in rows]
        return [Source.model_validate_json(r[0]) for r in db.execute("SELECT body FROM sources")]

    def candidates(self, db):
        if self.is_pg:
            rows = db.execute("SELECT object_key,object_sha256,object_size FROM diamond.candidates "
                              "ORDER BY created_at DESC,id")
            return [Candidate.model_validate(self.object(*r)) for r in rows]
        return [Candidate.model_validate_json(r[0]) for r in db.execute(
            "SELECT body FROM candidates ORDER BY rowid DESC")]

    def run_rows(self, db):
        if self.is_pg:
            return list(db.execute("SELECT id,body,updated_at,http_attempts FROM diamond.runs "
                                   "ORDER BY updated_at DESC,id"))
        return [(r[0], json.loads(r[1]), None, None) for r in db.execute(
            "SELECT id,body FROM runs ORDER BY rowid DESC")]

    def papers(self, db, sources):
        papers = [PaperRow(id=s.id, title=s.title, domain="", journal="", uri=s.uri,
                           sha256=s.sha256, kind=s.kind) for s in sources]
        if not self.is_pg:
            return papers
        if not db.execute("SELECT to_regclass('diamond.library_papers')").fetchone()[0]:
            return papers
        rows = db.execute("""SELECT p.id,p.title,p.domain,p.journal,p.doi,
            count(d.id),coalesce(sum(d.pages),0)
            FROM diamond.library_papers p
            LEFT JOIN diamond.library_paper_documents pd ON pd.paper_id=p.id
            LEFT JOIN diamond.library_documents d ON d.id=pd.document_id
            GROUP BY p.id ORDER BY p.title,p.id""")
        papers += [PaperRow(id=r[0], title=r[1], domain=r[2], journal=r[3], doi=r[4],
                            documents=r[5], pages=r[6], kind="library") for r in rows]
        return papers

    def archive_reports(self):
        # 仅显式指定的脱敏报告目录；不递归扫描work/private或运行环境文件。
        reports = {}
        warnings = []
        if self.reports and self.reports.is_dir():
            for path in sorted(self.reports.glob("*.json")):
                try:
                    if path.is_symlink() or path.stat().st_size > 5_000_000:
                        continue
                    body = json.loads(path.read_text())
                except (OSError, ValueError):
                    warnings.append(f"档案 {path.name} 读取失败；其他报告仍可浏览。")
                    continue
                if not isinstance(body, dict) or not isinstance(body.get("run_id"), str):
                    continue
                if not isinstance(body.get("cost"), dict):
                    continue
                run_id = body["run_id"]
                prior = reports.get(run_id)
                if prior and str(prior[1].get("finalized_at", "")) > str(body.get("finalized_at", "")):
                    continue
                reports[run_id] = (path.name, body)
        return reports, warnings

    @staticmethod
    def archive_row(filename, body):
        cost = body["cost"]
        return ArchiveRow(
            id=body["run_id"], filename=filename, status=body.get("status", "unknown"),
            finalized_at=body.get("finalized_at"),
            planned=body.get("planned_lines", body.get("planned_lineages")),
            formed=body.get("formed_question_versions", body.get("formed_versions")),
            retained=body.get("delivery_items"), known_cost_usd=cost.get("known_cost_usd"),
            http_attempts=cost.get("http_attempts"), unknown_usage=cost.get("unknown_usage_attempts"),
            unsettled=cost.get("unsettled_reserved_attempts"), by_role=cost.get("by_role", {}),
            pricing_date=cost.get("pricing_snapshot_date"),
        )

    def snapshot(self):
        warnings = []
        with self.connection() as db:
            sources = self.sources(db)
            source_map = {s.id: s for s in sources}
            questions = [summarize(c, source_map) for c in self.candidates(db)]
            counts = Counter(q.run_id for q in questions)
            accepted = Counter(q.run_id for q in questions if q.status == "accepted")
            runs = [RunRow(id=rid, status=body.get("status", "unknown"), mode=body.get("mode", "unknown"),
                           fingerprint=body.get("fingerprint", ""), updated_at=str(at) if at else None,
                           candidates=counts[rid], accepted=accepted[rid], http_attempts=http)
                    for rid, body, at, http in self.run_rows(db)]
            papers = self.papers(db, sources)
        archives = []
        reports, archive_warnings = self.archive_reports()
        warnings.extend(archive_warnings)
        for name, body in reports.values():
            try:
                archives.append(self.archive_row(name, body))
            except (ValueError, TypeError):
                warnings.append(f"档案 {name} 结构校验失败；未将缺失费用记为零。")
        return Snapshot(generated_at=datetime.now(UTC).isoformat(),
                        backend="postgresql" if self.is_pg else "sqlite", runs=runs,
                        questions=questions, papers=papers,
                        archives=sorted(archives, key=lambda a: a.finalized_at or "", reverse=True),
                        statuses=dict(Counter(q.status for q in questions)), warnings=warnings)

    def question(self, identity):
        with self.connection() as db:
            if self.is_pg:
                row = db.execute("SELECT object_key,object_sha256,object_size FROM diamond.candidates "
                                 "WHERE id=%s", (identity,)).fetchone()
                candidate = Candidate.model_validate(self.object(*row)) if row else None
            else:
                row = db.execute("SELECT body FROM candidates WHERE id=?", (identity,)).fetchone()
                candidate = Candidate.model_validate_json(row[0]) if row else None
            if candidate is None:
                raise KeyError(identity)
            sources = {s.id: s for s in self.sources(db)}
            return {"summary": summarize(candidate, sources).model_dump(),
                    "candidate": candidate.model_dump(),
                    "sources": [s.model_dump(exclude={"text"}) for s in sources.values()
                                if s.id in candidate.source_ids]}

    def run(self, identity):
        with self.connection() as db:
            rows = [r for r in self.run_rows(db) if r[0] == identity]
            if not rows:
                raise KeyError(identity)
            _, body, _, _ = rows[0]
            # 只返回运行身份与协议字段，不透传任意运行body或环境配置。
            settings = body.get("settings", {})
            roles = {role: {k: v for k, v in settings.get(role, {}).items()
                            if k in {"model", "reasoning_effort", "temperature", "top_p"}}
                     for role in ("generator", "auditor", "consistency", "solver", "judge")}
            return {"id": identity, "status": body.get("status"), "mode": body.get("mode"),
                    "fingerprint": body.get("fingerprint"), "models": roles,
                    "protocol": {k: settings[k] for k in (
                        "synthesis_protocol", "source_fidelity", "samples", "early_screen_samples",
                        "difficulty_policy", "require_verifier_adversary", "freeze_question_at_verifier",
                        "solver_deadline_counts_as_incorrect", "evaluation_mode") if k in settings},
                    "prompt_hashes": body.get("prompt_hashes", {})}

    def archive(self, identity):
        reports, _ = self.archive_reports()
        entry = reports.get(identity)
        if entry is None:
            raise KeyError(identity)
        return {"filename": entry[0], "report": entry[1]}
