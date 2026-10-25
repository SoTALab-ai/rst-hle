"""独立个人VM上验证两阶段出题/质检；禁止任何solver调用，不进行难度验收。"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path

from pydantic import Field

from diamond_factory.config import load
from diamond_factory.costs import MeteredGateway, measure_cost
from diamond_factory.difficulty import construction_ready
from diamond_factory.difficulty_adversary import summarize_difficulty
from diamond_factory.llm import ModelResponseError
from diamond_factory.models import Source, StrictModel, digest
from diamond_factory.pipeline import AuthoringRejected, Pipeline
from diamond_factory.postgres import open_store
from diamond_factory.runtime import atomic_json


class ProbeCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    source: Source
    category: str
    subtopic: str


class ProbeGateway(MeteredGateway):
    async def call(self, role, *args, **kwargs):
        if role == "solver":
            raise RuntimeError("Difficulty probe must not buy GLM answers")
        return await super().call(role, *args, **kwargs)


async def run(args):
    settings = load(args.config)
    if not settings.require_difficulty_adversary:
        raise ValueError("This probe requires both new difficulty stages")
    cases = [ProbeCase.model_validate(v) for v in json.loads(Path(args.cases).read_text())]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Nonempty unique probe cases required")
    pricing = json.loads(Path(args.rates).read_text())
    store = open_store(Path(args.directory) / "unused.sqlite")
    if not hasattr(store, "get_request"):
        raise ValueError("Real probe requires an isolated PostgreSQL and CAS")
    report = {"run_prefix": args.run_prefix, "pilot_only": True, "new_glm_requests": 0,
              "difficulty_samples": 0, "difficulty_accepted": 0, "cases": []}
    try:
        with store.lock():
            # 所有case共用同一冻结来源集合，避免后加来源导致先前run的续跑指纹变化。
            for case in cases:
                store.add_source(case.source)
            for case in cases:
                run_id = args.run_prefix + "-" + case.id
                directory = Path(args.directory) / case.id
                directory.mkdir(parents=True, exist_ok=True)
                gateway = ProbeGateway(settings, directory / "requests", store, run_id,
                    pricing=pricing, visible_cost_limit_usd=None)
                pipe = Pipeline(settings, store, run_id, "pilot", directory, gateway)
                target = {"category": case.category, "subtopic": case.subtopic}
                pipe.start({"command": "two-stage-difficulty-probe", "case_sha256": digest(case.model_dump()),
                            "pricing_sha256": digest(pricing)})
                row = {"id": case.id, "run_id": run_id, "source_id": case.source.id,
                       "source_sha256": case.source.sha256, "category": case.category,
                       "preflight_passed": False, "difficulty_accepted": False}
                with (directory / "events-private.log").open("a") as stream, contextlib.redirect_stdout(stream):
                    try:
                        candidate = await pipe.generate(case.source, None, target, 0, 0)
                        row.update(candidate_id=candidate.id, preflight_passed=construction_ready(candidate,
                            {case.source.id: case.source}, settings, pipe.prompts), reasons=candidate.reasons)
                        pipe.metadata("completed" if row["preflight_passed"] else "completed_with_errors")
                    except (AuthoringRejected, ModelResponseError) as error:
                        row.update(error_type=type(error).__name__, error=str(error)[:300])
                        pipe.metadata("completed_with_errors")
                own = [c for c in store.candidates() if c.run_id == run_id]
                row["stages"] = summarize_difficulty(pipe, own)
                row["cost"] = measure_cost(store, run_id, directory / "requests", settings, pricing)
                report["cases"].append(row)
                report["known_cost_usd"] = sum(c["cost"]["known_cost_usd"] for c in report["cases"])
                report["unknown_usage_attempts"] = sum(c["cost"]["unknown_usage_attempts"] for c in report["cases"])
                atomic_json(Path(args.directory) / "probe-report.json", report)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        store.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/library-difficulty-adversary.toml")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--rates", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--run-prefix", required=True)
    asyncio.run(run(parser.parse_args()))
