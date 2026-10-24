"""正式元数据登记/去重→开放PDF下载→6Luna读取→PG与内容库入库。"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from diamond_factory.campaign_costs import CostStage, campaign_costs
from diamond_factory.config import load
from diamond_factory.models import digest
from diamond_factory.paper_acquisition import acquire_library
from diamond_factory.paper_ingest import LibraryPlan, LibraryRepository, ingest_library
from diamond_factory.postgres import PostgresStore
from diamond_factory.runtime import atomic_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--discovery', required=True)
    p.add_argument('--rates', required=True)
    p.add_argument('--directory', required=True)
    p.add_argument('--run-prefix', required=True)
    p.add_argument('--objects', required=True)
    p.add_argument('--config', default='configs/paper-reader.toml')
    p.add_argument('--target-per-domain', type=int, default=100)
    p.add_argument('--reuse-root')
    p.add_argument('--preferred-reuse-root')
    p.add_argument('--cache-root')
    p.add_argument('--representative-checks', help='已人工核对原PDF的开发反例清单；不代表公式认证')
    a = p.parse_args()
    if a.target_per_domain < 1 or not a.run_prefix or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in a.run_prefix):
        raise ValueError('Require a positive target and safe run prefix')
    directory = Path(a.directory)
    settings, pricing = load(a.config), json.loads(Path(a.rates).read_text())
    discovery = json.loads(Path(a.discovery).read_text())
    store = PostgresStore(os.environ['DIAMOND_DATABASE_URL'], a.objects)
    store.objects.compress_json = True
    acquisition_id, reading_id = a.run_prefix+'-acquire', a.run_prefix+'-read'
    stages = [CostStage(scope='library', stage=name, run_id=rid, directory=str(directory/name),
                        config=a.config, pricing=pricing) for name, rid in
              [('acquisition', acquisition_id), ('reading', reading_id),
               ('representative', a.run_prefix+'-representative')]]

    async def execute():
        acquired = await acquire_library(store, discovery, settings, pricing, directory/'acquisition',
            acquisition_id, reuse_root=a.reuse_root, preferred_reuse_root=a.preferred_reuse_root,
            target_per_domain=a.target_per_domain, cache_root=a.cache_root)
        # LibraryPlan再次拒绝同DOI不同PDF版本；计数达标仍需唯一性和原件校验。
        plan = LibraryPlan.model_validate_json((directory/'acquisition/library-plan.json').read_text())
        if acquired['status'] != 'downloaded' or len(plan.papers) != a.target_per_domain*2:
            atomic_json(directory/'pipeline-report.json', {'status': 'acquisition_incomplete',
                'acquisition': acquired, 'reading': None})
            return
        atomic_json(directory/'handoff.json', {'run_prefix': a.run_prefix,
            'plan_sha256': digest(plan.model_dump()), 'unique_papers': len(plan.papers),
            'reading_run_id': reading_id})
        if a.representative_checks:
            checks = json.loads(Path(a.representative_checks).read_text())
            selected = [paper for paper in plan.papers if paper.doi in {c['doi'] for c in checks}]
            if len(selected) != len({c['doi'] for c in checks}):
                raise ValueError('Representative check paper absent from frozen cohort')
            probe = await ingest_library(store, LibraryPlan(expected_papers=len(selected), papers=selected),
                settings, pricing, directory/'representative', a.run_prefix+'-representative', pages_per_call=2)
            if probe['failures'] or any(d['missing_pages'] for d in probe['documents']):
                raise ValueError('Representative real model/coverage failure; bulk reading blocked')
            repo = LibraryRepository(store)
            observed = []
            for check in checks:
                paper = next(p for p in selected if p.doi == check['doi'])
                if paper.documents[0].sha256 != check['original_pdf_sha256']:
                    raise ValueError('Representative original PDF identity changed')
                doc = next(d for d in repo.documents(probe['run_id']) if d[1] == check['original_pdf_sha256'])
                page = next(p for p in repo.pages(doc[0]) if p.page == check['page'])
                preserved = check['literal'] in page.text
                observed.append({'check': check, 'literal_preserved': preserved, 'page_status': page.status})
                if not preserved and page.status != 'unreadable':
                    atomic_json(directory/'representative/checks.json', {'passed': False, 'observed': observed})
                    raise ValueError('Known source spelling regression persists without uncertainty flag')
            atomic_json(directory/'representative/checks.json', {'passed': True, 'observed': observed,
                'full_formula_accuracy_certified': False})
        reading = await ingest_library(store, plan, settings, pricing, directory/'reading', reading_id, pages_per_call=2)
        atomic_json(directory/'pipeline-report.json', {'status': reading['status'],
            'acquisition': acquired, 'reading': reading,
            'cost': campaign_costs({'library': store}, stages)})
    try:
        with store.lock():
            asyncio.run(execute())
    finally:
        # 中断也输出已发生费用；状态/未知调用不能冒充成功或免费。
        try:
            atomic_json(directory/'cost-report.json', campaign_costs({'library': store}, stages))
        finally:
            store.db.close()


if __name__ == '__main__':
    main()
