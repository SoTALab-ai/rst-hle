import argparse
import asyncio
import json
from pathlib import Path

from .config import load
from .gates import correct_count, production_blockers, static_errors
from .models import Submission, digest
from .pipeline import Pipeline, report
from .postgres import open_store
from .sources import crawl_arxiv, import_sources


def main():
    parser = argparse.ArgumentParser(description="RST-inspired question factory; QC first")
    parser.add_argument("--db", default="work/factory.sqlite")
    parser.add_argument("--config", default="configs/pilot.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Import full-text JSONL, local Markdown or PDF manifest")
    ingest.add_argument("manifest")
    crawl = sub.add_parser("crawl", help="Search/download arXiv full-text papers")
    crawl.add_argument("--query")
    crawl.add_argument("--ids", nargs="*", default=[])
    crawl.add_argument("--limit", type=int, default=1)
    crawl.add_argument("--directory", default="work/papers")
    for command in ("run", "qc"):
        run = sub.add_parser(command, help="Evolve questions" if command == "run" else "QC existing authored bundles")
        run.add_argument("--run-id", required=True)
        run.add_argument("--mode", choices=["pilot", "production"], default="pilot")
        if command == "run":
            run.add_argument("--subtopic")
        else:
            run.add_argument("submission")
    sub.add_parser("doctor", help="Check procurement configuration; makes no model calls")
    sub.add_parser("report")
    sub.add_parser("db-init", help="Explicitly apply PostgreSQL schema; startup never migrates")
    schema = sub.add_parser("schema")
    schema.add_argument("--output", default="outputs/submission.schema.json")
    export = sub.add_parser("export", help="Export accepted independent items; optional exact full-batch check")
    export.add_argument("--output", default="outputs/delivery.jsonl")
    export.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    settings = load(args.config)
    quotas = json.loads(Path(settings.quotas_path).read_text())
    store = open_store(args.db, initialize=args.command == "db-init")
    if args.command == "db-init":
        store.migrate()
        store.save_quotas(quotas)
        print("PostgreSQL schema initialized and quota manifest stored")
        return
    # Linux/macOS lock is process-scoped, automatically released on exit or interruption.
    with store.lock():
        if args.command == "schema":
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(Submission.model_json_schema(), ensure_ascii=False, indent=2))
        elif args.command in ("ingest", "crawl"):
            sources = (import_sources(args.manifest) if args.command == "ingest" else
                       crawl_arxiv(args.query, args.ids, args.limit, Path(args.directory)))
            for source in sources:
                store.add_source(source)
                print(json.dumps({"imported": source.id, "title": source.title,
                                  "characters": len(source.text), "sha256": source.sha256}, ensure_ascii=False))
        elif args.command == "doctor":
            print(json.dumps({"blockers": production_blockers(settings, quotas),
                              "external_dedup_required": settings.external_dedup_required,
                              "configured_external_corpora": len(settings.dedup_corpora),
                              "sources": len(store.sources()), "quotas": len(quotas["quotas"]),
                              "target": quotas["target"]}, ensure_ascii=False, indent=2))
        elif args.command in ("run", "qc"):
            if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
                raise ValueError("run-id must contain only letters, numbers, underscore or hyphen")
            directory = Path("work/runs") / args.run_id
            pipeline = Pipeline(settings, store, args.run_id, args.mode, directory)
            if args.command == "qc":
                submissions = [Submission.model_validate_json(line) for line in
                               Path(args.submission).read_text().splitlines() if line.strip()]
                result = asyncio.run(pipeline.qc(submissions))
            else:
                result = asyncio.run(pipeline.run(args.subtopic))
            Path("outputs").mkdir(exist_ok=True)
            Path("outputs", args.run_id + ".report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "report":
            print(json.dumps(report(store, quotas), ensure_ascii=False, indent=2))
        elif args.command == "export":
            result = report(store, quotas)
            if args.require_complete and any(row["remaining"] for row in result["remaining"]):
                raise ValueError("All 61 subtopic quotas must be met separately; batch is incomplete")
            sources = {s.id: s for s in store.sources()}
            rows = []
            for c in store.candidates():
                if c.status != "accepted":
                    continue
                if static_errors(c, sources, quotas) or not c.audit.passed() or not 1 <= correct_count(c.trials) <= 8:
                    raise ValueError("Stored candidate failed export revalidation: " + c.id)
                rows.append({"id": c.id, **c.item.model_dump(), "lineage": {"root": c.root_id,
                             "parent": c.parent_id, "generation": c.generation},
                             "correct_count": correct_count(c.trials),
                             "trials": [t.model_dump() for t in c.trials], "run_id": c.run_id})
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            scope = {"items": len(rows), "complete": not any(r["remaining"] for r in result["remaining"]),
                     "external_dedup": "configured_corpora" if settings.dedup_corpora else "skipped_per_user_request",
                     "dataset_hash": digest(rows), "runs": store.runs()}
            target.with_suffix(".manifest.json").write_text(json.dumps(scope, ensure_ascii=False, indent=2))
            if hasattr(store, "export"):
                store.export(scope)
            print(json.dumps({"exported": len(rows), "path": str(target)}))


if __name__ == "__main__":
    main()
