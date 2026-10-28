import argparse
import asyncio
import json
from pathlib import Path

from .calibration import BenchmarkQuestion, Calibration
from .config import load
from .consistency import has_valid_consistency
from .difficulty import construction_ready
from .gates import can_count, correct_count, production_blockers, sample_outcomes, static_errors
from .models import Submission, digest
from .pipeline import Pipeline, report
from .postgres import open_store
from .routing import route_sources
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
    for command in ("run", "qc", "preflight"):
        run = sub.add_parser(command, help={"run": "Evolve questions", "qc": "QC existing authored bundles",
                                           "preflight": "Check correctness without difficulty sampling"}[command])
        run.add_argument("--run-id", required=True)
        run.add_argument("--mode", choices=["pilot", "production"], default="pilot")
        if command == "run":
            run.add_argument("--subtopic")
        else:
            run.add_argument("submission")
    sub.add_parser("doctor", help="Check procurement configuration; makes no model calls")
    sub.add_parser("report")
    sub.add_parser("db-init", help="Explicitly apply PostgreSQL schema; startup never migrates")
    catalog = sub.add_parser("catalog-import", help="Import publication metadata after importing full texts")
    catalog.add_argument("manifest")
    papers = sub.add_parser("papers", help="Query the versioned paper catalog; no model calls")
    papers.add_argument("--domain", choices=["Math", "Physics"])
    papers.add_argument("--journal")
    papers.add_argument("--theory-only",action="store_true")
    fetch = sub.add_parser("catalog-fetch", help="Collect full texts and inert LaTeX for a pinned paper catalog")
    fetch.add_argument("manifest")
    fetch.add_argument("--directory",default="work/papers")
    fetch.add_argument("--pdf-only",action="store_true")
    route = sub.add_parser("route-sources", help="Classify the source library before generation")
    route.add_argument("--run-id", required=True)
    route.add_argument("--output")
    calibrate = sub.add_parser("calibrate", help="Evaluate reference HLE questions without delivery admission")
    calibrate.add_argument("input")
    calibrate.add_argument("--run-id", required=True)
    calibrate.add_argument("--reuse-run", help="Reuse identical requests from a prior reference run; settings must match")
    calibrate.add_argument("--max-new-samples", type=int,
                           help="Checkpoint after this many new question/sample pairs; classification still requires 16")
    schema = sub.add_parser("schema")
    schema.add_argument("--output", default="outputs/submission.schema.json")
    export = sub.add_parser("export", help="Export accepted independent items; optional exact full-batch check")
    export.add_argument("--output", default="outputs/delivery.jsonl")
    export.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    if hasattr(args, "run_id") and (not args.run_id or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id)):
        raise ValueError("Invalid run-id")
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
        elif args.command == "catalog-import":
            from .catalog import read_catalog, save_catalog
            rows = read_catalog(args.manifest)
            save_catalog(store,rows)
            print(json.dumps({"cataloged_papers":len(rows)}))
        elif args.command == "papers":
            from .catalog import catalog_rows
            print(json.dumps(catalog_rows(store,args.domain,args.journal,args.theory_only),ensure_ascii=False,indent=2))
        elif args.command == "catalog-fetch":
            from .catalog import fetch_catalog_sources, read_catalog
            for row in fetch_catalog_sources(store,read_catalog(args.manifest),args.directory,not args.pdf_only):
                print(json.dumps(row,ensure_ascii=False),flush=True)
        elif args.command == "route-sources":
            output = args.output or settings.source_routes_path or "work/source-routes.json"
            book = asyncio.run(route_sources(settings, store, args.run_id, output))
            print(json.dumps({"routed_sources": len(book["sources"]), "output": output}))
        elif args.command == "calibrate":
            if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
                raise ValueError("Invalid run-id")
            questions = [BenchmarkQuestion.model_validate_json(line) for line in
                         Path(args.input).read_text().splitlines() if line.strip()]
            result = asyncio.run(Calibration(settings, store, args.run_id,
                                            Path("work/runs") / args.run_id,
                                            reuse_run=args.reuse_run).run(questions, args.max_new_samples))
            Path("outputs").mkdir(exist_ok=True)
            Path("outputs", args.run_id + ".json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({"by_category": result["by_category"], "totals": result["totals"]},
                             ensure_ascii=False, indent=2))
        elif args.command in ("run", "qc", "preflight"):
            if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
                raise ValueError("run-id must contain only letters, numbers, underscore or hyphen")
            directory = Path("work/runs") / args.run_id
            pipeline = Pipeline(settings, store, args.run_id, args.mode, directory)
            if args.command in ("qc", "preflight"):
                submissions = [Submission.model_validate_json(line) for line in
                               Path(args.submission).read_text().splitlines() if line.strip()]
                result = asyncio.run(pipeline.qc(submissions, preflight_only=args.command == "preflight"))
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
            accepted = [c for c in store.candidates() if c.status == "accepted"]
            for c in accepted:
                verifier = Pipeline(settings, store, c.run_id, "production", Path("work/runs") / c.run_id)
                verifier.validate_deadlines(c)
                # 用Pipeline实际装配的提示（含模式替换与补充条款）复核收据，避免与生成时提示不一致。
                if (static_errors(c, sources, quotas) or not c.audit.passed(basis=c.correctness_basis) or
                        not construction_ready(c, sources, settings, verifier.prompts) or
                        not has_valid_consistency(c, sources) or
                        not 1 <= correct_count(sample_outcomes(c)) <= 8 or
                        can_count(c, accepted, quotas, settings.max_per_root)):
                    raise ValueError("Stored candidate failed export revalidation: " + c.id)
                rows.append({"id": c.id, **c.item.model_dump(), "lineage": {"root": c.root_id,
                             "parent": c.parent_id, "generation": c.generation},
                             "correct_count": correct_count(sample_outcomes(c)),
                             "source_fidelity": c.source_fidelity, "source_notes": c.source_notes,
                             "consistency": c.consistency.model_dump(),
                             "verifier_history": [a.model_dump() for a in c.verifier_history],
                             "verifier_assessment": (c.verifier_assessment.model_dump()
                                                     if c.verifier_assessment else None),
                             "trials": [t.model_dump() for t in c.trials],
                             "deadline_outcomes": [d.model_dump() for d in c.deadline_outcomes], "run_id": c.run_id})
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            scope = {"items": len(rows), "complete": not any(r["remaining"] for r in result["remaining"]),
                     "external_dedup": "configured_corpora" if settings.dedup_corpora else "skipped_per_user_request",
                     "dataset_hash": digest(rows),
                     "runs": {key: value for key, value in store.runs().items()
                              if key in {c.run_id for c in accepted}}}
            target.with_suffix(".manifest.json").write_text(json.dumps(scope, ensure_ascii=False, indent=2))
            if hasattr(store, "export"):
                store.export(scope)
            print(json.dumps({"exported": len(rows), "path": str(target)}))


if __name__ == "__main__":
    main()
