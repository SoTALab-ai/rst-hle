"""在个人VM独立PG/CAS中对冻结论证题执行前置检查与4→16研究评测。"""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path

from diamond_factory.argument_study import PROTOCOL, ArgumentStudy
from diamond_factory.config import load
from diamond_factory.costs import MeteredGateway, measure_cost
from diamond_factory.models import digest
from diamond_factory.pipeline import Pipeline
from diamond_factory.postgres import PostgresStore, open_store
from diamond_factory.runtime import atomic_json

LOGGER = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--rates", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
        raise ValueError("Invalid run-id")
    manifest = Path(args.freeze)
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != args.freeze_sha256:
        raise ValueError("Frozen manifest changed")
    frozen = json.loads(manifest.read_text())

    def check_freeze():
        if any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h for p, h in frozen["files"].items()):
            raise ValueError("Frozen inputs or code changed; create a new run")

    check_freeze()
    settings = load(args.config)
    if (settings.request_deadline_seconds != 600 or settings.evaluation_mode != "no_tools"
            or settings.solver.max_tokens is not None):
        raise ValueError("This study requires no tools, 600-second wall deadline and no client solver token cap")
    pricing = json.loads(Path(args.rates).read_text())
    store = open_store("work/unused-offline.sqlite")
    if not isinstance(store, PostgresStore):
        raise TypeError("A personal VM isolated PostgreSQL/CAS environment is required")
    candidate = next(c for c in store.candidates() if c.id == args.candidate)
    if digest(candidate.model_dump()) != args.candidate_sha256:
        raise ValueError("Frozen candidate changed")
    directory = Path(args.directory)
    gateway = MeteredGateway(settings, directory / "requests", store, args.run_id,
                             pricing=pricing, visible_cost_limit_usd=None)
    pipe = Pipeline(settings, store, args.run_id, "pilot", directory, gateway)
    failure = None
    with store.lock():
        pipe.start({"command": PROTOCOL, "candidate_sha256": args.candidate_sha256,
                    "freeze_sha256": args.freeze_sha256})
        study = ArgumentStudy(pipe, candidate)
        try:
            asyncio.run(study.run())
            pipe.metadata("completed")
        except Exception as error:
            LOGGER.exception("Argument study did not complete")
            failure = {"kind": type(error).__name__, "reason": str(error)[:700]}
            pipe.metadata("completed_with_errors")
        check_freeze()
        report = {**study.report(), "run_id": args.run_id, "failure": failure,
                  "code_commit": frozen["code_commit"], "frozen_changed": [],
                  "cost": measure_cost(store, args.run_id, directory / "requests", settings, pricing)}
        atomic_json(directory / "argument-study-report.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
    store.db.close()
    if failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
