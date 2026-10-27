"""RST独立实验：已验证题解逐轮扩展，对齐题面/verifier后再实测难度。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from diamond_factory.config import load
from diamond_factory.continuation import ContinuationGateway, import_solver_history
from diamond_factory.costs import MeteredGateway
from diamond_factory.models import Source
from diamond_factory.postgres import open_store
from diamond_factory.rst_curriculum import RstCurriculum, RstPlan
from diamond_factory.runtime import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='configs/rst-curriculum.toml')
    parser.add_argument('--plan', required=True)
    parser.add_argument('--sources', required=True)
    parser.add_argument('--rates', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--reuse-evidence', type=Path)
    args = parser.parse_args()
    if not args.run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in args.run_id):
        raise ValueError('Invalid run-id')
    settings = load(args.config)
    plan = RstPlan.model_validate_json(Path(args.plan).read_text())
    sources = [Source.model_validate(s) for s in json.loads(Path(args.sources).read_text())]
    if len({s.id for s in sources}) != len(sources):
        raise ValueError('Source IDs must be unique')
    if bool(args.reuse_evidence) != bool(plan.reuse_manifest_sha256):
        raise ValueError('Historical reuse must bind a sealed manifest in the plan')
    pricing = json.loads(Path(args.rates).read_text())
    directory = Path(args.directory)
    manifest = {'run_id': args.run_id, 'settings': settings.model_dump(), 'plan': plan.model_dump(),
        'sources': {s.id: s.sha256 for s in sources}, 'pricing': pricing,
        'entrypoint_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    frozen = directory / 'manifest.json'
    if frozen.exists() and json.loads(frozen.read_text()) != manifest:
        raise ValueError('Frozen RST campaign changed; use a new run-id')
    store = open_store(directory / 'unused.sqlite')
    if not hasattr(store, 'get_request'):
        raise ValueError('Real RST curriculum requires an isolated personal PostgreSQL and CAS')
    try:
        with store.lock():
            atomic_json(frozen, manifest)
            for source in sources:
                store.add_source(source)
            factory, extra = MeteredGateway, {}
            if args.reuse_evidence:
                candidates, traces = import_solver_history(store, args.reuse_evidence, plan.reuse_manifest_sha256, args.run_id)
                factory, extra = ContinuationGateway, {'historical_candidates': candidates, 'historical_traces': traces}
            gateway = factory(settings, directory / 'requests', store, args.run_id, pricing=pricing,
                visible_cost_limit_usd=plan.visible_cost_limit_usd, **extra)
            result = asyncio.run(RstCurriculum(settings, store, args.run_id, 'pilot', directory,
                gateway, plan=plan, pricing=pricing).run_curriculum())
            print(json.dumps({'event': 'rst_curriculum_finished', 'run_id': args.run_id,
                'formed_versions': result['formed_versions'], 'retained_lineages': result['retained_lineages'],
                'known_cost_usd': result['cost']['known_cost_usd']}, ensure_ascii=False), flush=True)
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
