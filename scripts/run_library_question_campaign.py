"""等待完整解析材料达到里程碑，冻结材料并通过原出题/质检/评测管线试产。"""
import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from diamond_factory.config import load
from diamond_factory.continuation import ContinuationGateway, import_solver_history
from diamond_factory.library_questions import (
    MaterialMilestone,
    complete_paper_ids,
    freeze_milestone,
    plan_questions,
    run_question_shards,
)
from diamond_factory.postgres import PostgresStore
from diamond_factory.runtime import atomic_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library-run-id', required=True)
    p.add_argument('--directory', required=True)
    p.add_argument('--run-prefix', required=True)
    p.add_argument('--rates', required=True)
    p.add_argument('--config', default='configs/library-shortcut-adversary.toml')
    p.add_argument('--materials', type=int, default=50, choices=range(1, 201))
    p.add_argument('--wait', action='store_true')
    p.add_argument('--exclude-milestone', type=Path, help='排除已试产材料；不改变原清单')
    p.add_argument('--reuse-evidence', type=Path, help='已封印solver/judge历史目录')
    p.add_argument('--reuse-manifest-sha256', help='历史manifest的文件SHA256，必须显式绑定')
    a = p.parse_args()
    if not a.run_prefix or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in a.run_prefix):
        raise ValueError('Invalid run prefix')
    library_url = os.environ['DIAMOND_LIBRARY_DATABASE_URL']
    question_url = os.environ['DIAMOND_DATABASE_URL']
    library_name = conninfo_to_dict(library_url).get("dbname")
    question_name = conninfo_to_dict(question_url).get("dbname")
    if not library_name or not question_name or library_name == question_name:
        raise ValueError('Library reading and question trial need separate personal databases')
    library = PostgresStore(library_url, os.environ['DIAMOND_LIBRARY_OBJECTS_DIR'])
    library.db.execute('SET default_transaction_read_only=on')
    store = PostgresStore(question_url, os.environ['DIAMOND_OBJECTS_DIR'])
    store.objects.compress_json = True
    settings = load(a.config)
    if not (settings.require_known_result and settings.require_stepwise_blueprint_review
            and settings.require_bottleneck_mask and settings.require_verifier_adversary
            and settings.difficulty_policy == 'ease_only' and settings.early_screen_samples == 4
            and settings.solver_deadline_counts_as_incorrect):
        raise ValueError('Question campaign requires the current full scientific/4→16/ease-only protocol')
    directory = Path(a.directory)
    try:
        with store.lock():
            excluded = []
            if a.exclude_milestone:
                previous = MaterialMilestone.model_validate_json(a.exclude_milestone.read_text())
                if previous.library_run_id != a.library_run_id:
                    raise ValueError('Excluded cohort belongs to a different library run')
                excluded = [p.id for p in previous.papers]
            if bool(a.reuse_evidence) != bool(a.reuse_manifest_sha256):
                raise ValueError('Historical reuse requires both evidence and its expected manifest SHA256')
            pricing = json.loads(Path(a.rates).read_text())
            frozen = {'run_prefix': a.run_prefix, 'library_run_id': a.library_run_id, 'materials': a.materials,
                'excluded_paper_ids': sorted(excluded), 'reuse_manifest_sha256': a.reuse_manifest_sha256,
                'settings': settings.model_dump(), 'pricing': pricing,
                'entrypoint_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
            manifest = directory/'campaign-manifest.json'
            if manifest.exists() and json.loads(manifest.read_text()) != frozen:
                raise ValueError('Frozen campaign changed; choose a new run-id')
            atomic_json(manifest, frozen)
            gateway_factory = None
            if a.reuse_evidence:
                candidates, traces = import_solver_history(store, a.reuse_evidence,
                    a.reuse_manifest_sha256, a.run_prefix)

                def gateway_factory(*args, **kwargs):
                    return ContinuationGateway(*args, historical_candidates=candidates,
                                               historical_traces=traces, **kwargs)

                gateway_factory(settings, directory/'reuse-check', store, a.run_prefix+'-reuse-check',
                                pricing=pricing, visible_cost_limit_usd=None)
            last = None
            while True:
                milestone = freeze_milestone(library, a.library_run_id, a.materials,
                                             directory/'milestone.json', exclude=excluded)
                if milestone:
                    break
                ready = len([pid for pid in complete_paper_ids(library, a.library_run_id, 200) if pid not in excluded])
                if ready != last:
                    print(json.dumps({'event': 'waiting_for_materials', 'required': a.materials,
                                      'complete_materials': ready}), flush=True)
                    last = ready
                upstream = library.runs().get(a.library_run_id, {})
                if upstream.get('status') in ('transcribed', 'incomplete', 'interrupted'):
                    atomic_json(directory/'campaign-report.json', {'status': 'insufficient_complete_materials',
                        'complete_materials': ready, 'required': a.materials,
                        'upstream_status': upstream['status'], 'delivery_items': 0})
                    return
                if not a.wait:
                    return
                time.sleep(30)
            async def execute():
                plans, planning = await plan_questions(store, milestone, settings, pricing,
                    directory/'planning', a.run_prefix+'-planning')
                reports = await run_question_shards(store, settings, pricing, plans,
                    directory/'questions', a.run_prefix, gateway_factory=gateway_factory)
                print(json.dumps({'event': 'campaign_finished', 'materials': len(milestone.papers),
                    'planned_roots': planning['planned_independent_roots'],
                    'generated_versions': sum(r['generated_versions'] for r in reports),
                    'retained_roots': sum(r['roots_with_retained_question'] for r in reports),
                    'delivery_items': 0}), flush=True)
            asyncio.run(execute())
    finally:
        library.db.close()
        store.db.close()


if __name__ == '__main__':
    main()
