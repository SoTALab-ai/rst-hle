"""Discover publication metadata into a review queue, separately from admitted full texts."""
import json
import re
import time
from datetime import UTC, date, datetime
from urllib.parse import quote

import httpx
from pydantic import Field

from .models import StrictModel, digest


class Journal(StrictModel):
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]*$')
    journal: str = Field(min_length=1)
    issn: str = Field(pattern=r'^\d{4}-\d{3}[\dX]$')
    domain: str = Field(pattern=r'^(Math|Physics)$')


def discover(journals, *, since, until, per_journal=20, client=None, pause=time.sleep):
    """Bounded DOI discovery; no inference that a journal article is theoretical or HLE-suitable."""
    first,last=date.fromisoformat(since),date.fromisoformat(until)
    if first>last or last>datetime.now(UTC).date() or not 1<=per_journal<=100:
        raise ValueError('Require a past/current date window and 1..100 records per journal')
    if (not 1<=len(journals)<=30 or len({j.id for j in journals})!=len(journals) or
        len({j.issn for j in journals})!=len(journals)):
        raise ValueError('Journal identifiers and ISSNs must be unique')
    owned=client is None
    client=client or httpx.Client(timeout=30,headers={
        'User-Agent':'rst-hle/0.1 (https://github.com/SoTALab-ai/rst-hle)'})
    rows={};failures=[];skipped=0
    try:
        for index,journal in enumerate(journals):
            if index:pause(1.1)
            endpoint='https://api.crossref.org/journals/'+quote(journal.issn,safe='')+'/works'
            params={'filter':f'from-pub-date:{since},until-pub-date:{until},type:journal-article',
                    'sort':'published','order':'desc','rows':per_journal}
            try:
                # No unbounded body or executable HTML; the queue retains metadata only.
                with client.stream('GET',endpoint,params=params) as response:
                    response.raise_for_status();content=bytearray()
                    for part in response.iter_bytes():
                        content.extend(part)
                        if len(content)>8_000_000:raise ValueError('Crossref metadata exceeds 8MB cap')
                    payload=json.loads(content);url=str(response.url)
                if (not isinstance(payload,dict) or payload.get('status')!='ok' or
                    not isinstance(payload.get('message'),dict) or
                    not isinstance(payload['message'].get('items'),list)):
                    raise ValueError('Unexpected Crossref response schema')
                for raw in payload['message']['items']:
                    if not isinstance(raw,dict):
                        skipped+=1;continue
                    doi=str(raw.get('DOI','')).lower();titles=raw.get('title',[])
                    if (raw.get('type')!='journal-article' or not isinstance(raw.get('ISSN'),list) or
                        journal.issn not in raw['ISSN'] or
                        not re.fullmatch(r'10\.\d{4,9}/\S+',doi) or not isinstance(titles,list) or
                        not titles or not isinstance(titles[0],str)):
                        skipped+=1;continue
                    row={'doi':doi,'title':titles[0],'journal':journal.journal,'journal_id':journal.id,
                         'issns':raw['ISSN'],'domain':journal.domain,'authors':raw.get('author',[]),
                         'publication_dates':{k:raw[k] for k in ('published','published-online','published-print') if k in raw},
                         'publisher_url':'https://doi.org/'+doi,'metadata_source':url,
                         'metadata_sha256':digest(raw),'observed_at':datetime.now(UTC).isoformat(),
                         'theory_screen':'unreviewed','fulltext_status':'not_fetched','arxiv_id':None,
                         'updates':raw.get('update-to',[]),'abstract':raw.get('abstract'),
                         'license_metadata':raw.get('license',[]),
                         'admitted_to_paper_catalog':False}
                    rows.setdefault(doi,row)
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as error:
                retryable=isinstance(error,httpx.TransportError) or (isinstance(error,httpx.HTTPStatusError) and
                    (error.response.status_code==429 or error.response.status_code>=500))
                failures.append({'journal_id':journal.id,'error_type':type(error).__name__,'retryable':retryable})
    finally:
        if owned:client.close()
    return {'window':{'since':since,'until':until},'per_journal':per_journal,'records':list(rows.values()),
            'failures':failures,'skipped_unverified_records':skipped,
            'note':'Metadata discovery queue only. Verify theory suitability, exact arXiv version, full text and rights before catalog admission.'}
