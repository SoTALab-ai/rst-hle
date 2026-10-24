"""正式论文入库入口：冻结清单→直接模型读取→PG索引/CAS；不运行出题。"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.paper_ingest import (
    LibraryPlan,
    LibraryRepository,
    ingest_library,
    inventory_directory,
    inventory_receipts,
)
from diamond_factory.postgres import PostgresStore
from diamond_factory.runtime import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    inventory = actions.add_parser("inventory")
    source = inventory.add_mutually_exclusive_group(required=True)
    source.add_argument("--receipts-root")
    source.add_argument("--pdf-directory")
    inventory.add_argument("--expected-papers", required=True, type=int)
    inventory.add_argument("--output", required=True)
    run = actions.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--directory", required=True)
    run.add_argument("--config", default="configs/paper-reader.toml")
    run.add_argument("--rates", required=True)
    run.add_argument("--pages-per-call", type=int, default=1, choices=(1, 2))
    export = actions.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--paper-id", required=True)
    export.add_argument("--output", required=True)
    for action in (run, export):
        action.add_argument("--objects", required=True)
    args = parser.parse_args()
    if args.action == "inventory":
        plan = (inventory_receipts(args.receipts_root, args.expected_papers) if args.receipts_root
                else inventory_directory(args.pdf_directory, args.expected_papers))
        path = Path(args.output)
        if path.exists() and json.loads(path.read_text()) != plan.model_dump():
            raise ValueError("Inventory output exists; choose a new plan path")
        atomic_json(path, plan.model_dump())
        print(json.dumps({"expected": plan.expected_papers, "located": len(plan.papers),
                          "pages": sum(d.pages for p in plan.papers for d in p.documents)}))
        return
    store = PostgresStore(os.environ["DIAMOND_DATABASE_URL"], args.objects)
    store.objects.compress_json = True
    try:
        with store.lock():
            if args.action == "export":
                text = LibraryRepository(store).paper_text(args.run_id, args.paper_id)
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text)
                return
            report = asyncio.run(ingest_library(store,
                LibraryPlan.model_validate_json(Path(args.plan).read_text()), load(args.config),
                json.loads(Path(args.rates).read_text()), Path(args.directory), args.run_id,
                pages_per_call=args.pages_per_call))
            print(json.dumps({k: report[k] for k in ("status", "expected_papers", "registered_papers",
                              "missing_input_papers", "unique_pdfs", "expected_pages", "stored_pages", "cost")}))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
