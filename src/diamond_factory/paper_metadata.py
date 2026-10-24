"""下载前登记规范化DOI身份，保留每份元数据观察；同论文版本不增加篇数。"""
import re
from copy import deepcopy

from psycopg.types.json import Jsonb

from .models import digest


def canonical_doi(value):
    if not isinstance(value, str):
        raise TypeError('DOI must be a string')
    doi = value.strip().casefold()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if doi.startswith(prefix):
            doi = doi[len(prefix):].strip()
            break
    if not re.fullmatch(r'10\.\d{4,9}/\S+', doi):
        raise ValueError('Invalid DOI identity')
    return doi


def deduplicate_metadata(records):
    """稳定保留首份规范化记录，重复来源另存；不凭标题相似误合并论文。"""
    unique, observations, duplicates = {}, [], []
    for raw in records:
        doi = canonical_doi(raw['doi'])
        row = deepcopy(raw)
        row['doi'] = doi
        observations.append({'doi': doi, 'metadata_sha256': digest(raw), 'body': deepcopy(raw)})
        if doi in unique:
            duplicates.append({'doi': doi, 'metadata_sha256': digest(raw), 'reason': 'duplicate_doi_metadata'})
        else:
            unique[doi] = row
    return list(unique.values()), observations, duplicates


def register_metadata(store, records, run_id):
    """PG主键使重启与多批元数据登记幂等；已下载缓存由采集收据继续验证。"""
    rows, observations, duplicates = deduplicate_metadata(records)
    known = []
    with store.db.transaction():
        for row in rows:
            result = store.db.execute('INSERT INTO diamond.library_metadata(doi) VALUES(%s) '
                'ON CONFLICT DO NOTHING RETURNING doi', (row['doi'],)).fetchone()
            if result is None:
                known.append(row['doi'])
        for obs in observations:
            store.db.execute('INSERT INTO diamond.library_metadata_observations '
                '(doi,metadata_sha256,body,first_run_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (obs['doi'], obs['metadata_sha256'], Jsonb(obs['body']), run_id))
    return rows, {'input_metadata_records': len(records), 'unique_dois': len(rows),
                  'duplicates_before_download': duplicates, 'previously_registered_dois': known,
                  'note': 'Metadata identity is not proof of a downloaded/readable/theoretically suitable paper.'}
