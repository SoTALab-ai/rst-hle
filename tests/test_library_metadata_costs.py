import asyncio
import json

import httpx
import pytest
from psycopg.errors import CheckViolation
from pypdf import PdfWriter
from test_paper_ingest import pg as pg_fixture

from diamond_factory.campaign_costs import CostStage, campaign_costs
from diamond_factory.config import load
from diamond_factory.costs import measure_cost
from diamond_factory.models import digest
from diamond_factory.paper_acquisition import acquire_library, import_reused_collections
from diamond_factory.paper_ingest import LibraryPlan, inventory_receipts
from diamond_factory.paper_metadata import canonical_doi, deduplicate_metadata, register_metadata
from diamond_factory.pdf_library import PaperPdfPlan, PdfInput, collect_paper


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def seed(root, name, doi, domain='Math'):
    pdf = root.parent/(name+'.pdf')
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_metadata({'/Title': name})
    writer.write(pdf)
    plan = PaperPdfPlan(doi=doi, title='Synthetic controlled paper '+name, journal='Fixture', domain=domain,
        metadata_url='https://example.org/metadata', documents=[PdfInput(role='main', location=str(pdf),
            version='publishedVersion', provenance_url='https://example.org/article')])
    with httpx.Client() as client:
        return collect_paper(plan, root, client=client)[0]


def pricing():
    return {'currency': 'USD', 'observed_at': '2026-10-23', 'rmb_reference_exchange_rate': 6.7,
        'rates_per_million': {model: {'input': 1, 'output': 2, 'cache_read': .1, 'cache_write': 1}
                             for model in ('gpt-6-luna', 'glm-5.2')}}


def test_metadata_dedup_preserves_observations_without_fuzzy_title_merges():
    rows, observations, dup = deduplicate_metadata([
        {'doi': '10.1000/ABC', 'title': 'A'}, {'doi': 'https://doi.org/10.1000/abc', 'title': 'Another version'},
        {'doi': '10.1000/distinct', 'title': 'A'}])
    assert [r['doi'] for r in rows] == ['10.1000/abc', '10.1000/distinct']
    assert len(observations) == 3 and len(dup) == 1
    assert canonical_doi(' DOI:10.1000/ABC ') == '10.1000/abc'
    with pytest.raises(ValueError, match='Invalid DOI'):
        canonical_doi('not-a-doi')


def test_metadata_registration_and_acquisition_do_not_redownload_versions(pg, tmp_path):
    preferred, prior = tmp_path/'seed', tmp_path/'prior'
    a = seed(preferred, 'main-a', '10.1000/SAME')
    seed(prior, 'version-a', '10.1000/same')
    seed(prior, 'physics', '10.1000/physics', 'Physics')
    settings = load('configs/paper-reader.toml')
    result = asyncio.run(acquire_library(pg, {'records': [{'doi': '10.1000/SAME', 'domain': 'Math',
        'journal_id': 'fixture', 'updates': []}, {'doi': '10.1000/same', 'domain': 'Math',
        'journal_id': 'fixture', 'updates': []}]}, settings, pricing(), tmp_path/'acq', 'metadata-test',
        reuse_root=prior, preferred_reuse_root=preferred, target_per_domain=1))
    assert result['downloaded_papers'] == 2 and result['cost']['http_attempts'] == 0
    plan = LibraryPlan.model_validate_json((tmp_path/'acq/library-plan.json').read_text())
    assert next(p for p in plan.papers if p.domain == 'Math').documents[0].sha256 == a['documents'][0]['pdf_sha256']
    assert len(result['reused_versions_skipped']) == 1
    assert result['metadata']['unique_dois'] == 2
    assert pg.db.execute('SELECT count(*) FROM diamond.library_metadata').fetchone()[0] == 2
    registered, report = register_metadata(pg, [{'doi': '10.1000/Same'}], 'metadata-test')
    assert registered[0]['doi'] == '10.1000/same'
    assert report['previously_registered_dois'] == ['10.1000/same']
    # 数据库主键也拒绝跳过规范化的绕过入口。
    with pytest.raises(CheckViolation, match='check constraint'):
        pg.db.execute("INSERT INTO diamond.library_metadata(doi) VALUES('10.1000/UPPER')")


def test_reused_corrupt_file_and_case_variant_library_plan_rejected(tmp_path):
    root = tmp_path/'prior'
    a = seed(root, 'a', '10.1000/SAME')
    seed(root, 'b', '10.1000/same')
    with pytest.raises(ValueError, match='Duplicate DOI'):
        inventory_receipts(root, 2)
    p = root/a['plan_sha256']/a['documents'][0]['pdf_path']
    p.write_bytes(b'broken')
    with pytest.raises(ValueError, match='Corrupt paper cache'):
        import_reused_collections(tmp_path/'accepted', [root], 100)


def test_billing_includes_orphan_failure_and_avoids_cache_rebilling(pg, tmp_path):
    settings = load('configs/paper-reader.toml')
    for run in ['paid', 'reuse']:
        pg.run(run, {'mode': 'test', 'status': 'running', 'fingerprint': digest(run)})
    attempt = {'run_id': 'paid', 'request_id': 'request', 'ordinal': 1, 'role': 'generator',
               'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}
    failed = {**attempt, 'request_id': 'failed', 'ordinal': 2, 'usage': {}, 'response_error': 'http_503'}
    pg.save_attempt('paid', attempt)
    pg.save_attempt('paid', failed)
    trace = {'request_id': 'request', 'role': 'generator', 'salt': 'test', 'actual_model': 'gpt-6-luna',
             'usage': attempt['usage'], 'attempts': [attempt]}
    pg.save_request('paid', trace)
    pg.save_request('reuse', trace)
    # 重复本地收据和PG索引不能使费用翻倍。
    folder = tmp_path/'paid/requests/attempts/request'
    folder.mkdir(parents=True)
    (folder/'1.json').write_text(json.dumps(attempt))
    paid = measure_cost(pg, 'paid', tmp_path/'paid/requests', settings, pricing())
    assert paid['http_attempts'] == 2 and paid['unknown_usage_attempts'] == 1
    assert paid['known_cost_usd'] == pytest.approx(.0002)
    stage = CostStage(scope='library', stage='download', run_id='paid', directory=str(tmp_path/'paid'),
                      config='configs/paper-reader.toml', pricing=pricing())
    report = campaign_costs({'library': pg}, [stage, stage.model_copy(update={'run_id': 'reuse'})])
    assert report['known_cost_usd'] == paid['known_cost_usd'] and report['http_attempts'] == 2
    assert report['infrastructure_cost_usd'] is None
    with pytest.raises(ValueError, match='Duplicate original billing run'):
        campaign_costs({'library': pg}, [stage, stage])
