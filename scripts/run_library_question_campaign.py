"""等待完整解析材料达到里程碑，冻结材料并通过原出题/质检/评测管线试产。"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from diamond_factory.config import load
from diamond_factory.library_questions import (
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
    p.add_argument('--config', default='configs/library50-questions.toml')
    p.add_argument('--materials', type=int, default=50, choices=range(1, 201))
    p.add_argument('--wait', action='store_true')
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
            last = None
            while True:
                milestone = freeze_milestone(library, a.library_run_id, a.materials, directory/'milestone.json')
                if milestone:
                    break
                ready = len(complete_paper_ids(library, a.library_run_id, a.materials))
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
            pricing = json.loads(Path(a.rates).read_text())

            async def execute():
                plans, planning = await plan_questions(store, milestone, settings, pricing,
                    directory/'planning', a.run_prefix+'-planning')
                reports = await run_question_shards(store, settings, pricing, plans,
                    directory/'questions', a.run_prefix)
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
