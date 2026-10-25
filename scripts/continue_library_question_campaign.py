"""个人VM新批次续产：复用冻结材料/作者输出/已完成答卷，不修改原实验。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.continuation import ContinuationExperiment, ContinuationGateway, prepare_continuation
from diamond_factory.models import digest
from diamond_factory.postgres import PostgresStore
from diamond_factory.runtime import atomic_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence", type=Path, required=True)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--run-prefix", required=True)
    p.add_argument("--rates", type=Path, required=True)
    p.add_argument("--baseline-cost", type=Path, required=True)
    p.add_argument("--config", type=Path, default=Path("configs/library-source-inspired.toml"))
    a = p.parse_args()
    if not a.run_prefix or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in a.run_prefix):
        raise ValueError("Invalid continuation run prefix")
    if a.directory.resolve().is_relative_to(a.evidence.resolve()):
        raise ValueError("Output must be outside frozen input")
    settings = load(a.config)
    if not (settings.source_fidelity == "advisory" and settings.require_known_result
            and settings.require_bottleneck_mask and settings.require_verifier_adversary
            and settings.require_stepwise_blueprint_review and settings.difficulty_policy == "ease_only"
            and settings.early_screen_samples == 4 and settings.solver_deadline_counts_as_incorrect):
        raise ValueError("Continuation requires the complete current advisory scientific protocol")
    pricing, baseline = json.loads(a.rates.read_text()), json.loads(a.baseline_cost.read_text())
    files = [p for root in (Path("src"), Path("prompts"), Path("configs"), Path("scripts"))
             for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".txt", ".json", ".toml", ".sql")]
    manifest = {"run_prefix": a.run_prefix, "input_manifest_sha256": a.manifest_sha256,
        "settings": settings.model_dump(), "pricing": pricing, "baseline_sha256": digest(baseline),
        "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}}
    path = a.directory/"manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("Frozen continuation changed; choose a new run-id and directory")
    atomic_json(path, manifest)
    store = PostgresStore(os.environ["DIAMOND_DATABASE_URL"], os.environ["DIAMOND_OBJECTS_DIR"])
    store.objects.compress_json = True
    try:
        with store.lock():
            async def execute():
                plan, prepared, historical, traces = await prepare_continuation(store, settings, pricing,
                    a.evidence, a.manifest_sha256, a.directory, a.run_prefix)
                print(json.dumps({"event": "continuation_prepared", "materials": prepared["materials"],
                    "active_roots": prepared["active_root_lines"],
                    "historical_closed_roots": len(prepared["historical_closed"]),
                    "reused_seed_roots": prepared["reused_seed_roots"]}), flush=True)
                if not plan:
                    return
                run_id = a.run_prefix+"-part01"
                gateway = ContinuationGateway(settings, a.directory/"questions"/"requests", store, run_id,
                    pricing=pricing, visible_cost_limit_usd=plan.visible_cost_limit_usd,
                    historical_candidates=historical, historical_traces=traces)
                experiment = ContinuationExperiment(settings, store, run_id, "pilot", a.directory/"questions",
                    gateway, plan=plan, pricing=pricing, preparation=prepared, baseline=baseline,
                    campaign_directory=a.directory)
                await experiment.run_experiment()
                print(json.dumps({"event": "continuation_finished", "report": str(a.directory/"continuation-report.json")}), flush=True)
            asyncio.run(execute())
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
