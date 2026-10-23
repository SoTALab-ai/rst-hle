"""One-process full-text -> routing -> generation -> QC pilot. No production quota admission."""
import argparse
import asyncio
import json
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.pipeline import Pipeline
from diamond_factory.postgres import open_store
from diamond_factory.routing import route_sources
from diamond_factory.runtime import atomic_json

parser = argparse.ArgumentParser()
parser.add_argument("--config", default="configs/pilot.toml")
parser.add_argument("--run-id", required=True)
args = parser.parse_args()
if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.run_id):
    raise ValueError("Invalid run-id")
settings = load(args.config)
store = open_store("work/factory.sqlite")


async def pilot():
    route_file = settings.source_routes_path or "work/source-routes.json"
    await route_sources(settings, store, args.run_id + "-routing", route_file)
    settings.source_routes_path = route_file
    pipe = Pipeline(settings, store, args.run_id, "pilot", Path("work/runs", args.run_id))
    result = await pipe.run()
    sources = {s.id: {"title": s.title, "uri": s.uri, "sha256": s.sha256, "kind": s.kind} for s in store.sources()}
    bundles = {"pilot_only": True, "delivery_items": 0, "sources": sources,
               "candidates": [c.model_dump() for c in store.candidates() if c.run_id == args.run_id]}
    atomic_json(Path("outputs", args.run_id + ".report.json"), result)
    atomic_json(Path("outputs", args.run_id + ".bundles.json"), bundles)
    print(json.dumps({"run_id": args.run_id, "candidate_count": len(bundles["candidates"]),
                      "statuses": result["statuses"], "delivery_items": 0}, ensure_ascii=False), flush=True)


with store.lock():
    asyncio.run(pilot())
