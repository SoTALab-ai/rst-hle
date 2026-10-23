import json

import httpx
import pytest

from diamond_factory.discovery import Journal, discover


def test_journal_discovery_verifies_issn_deduplicates_doi_and_never_admits_fulltexts():
    journal=Journal(id='fixture',journal='Synthetic journal',issn='0000-0000',domain='Math')
    work={'DOI':'10.1000/Fixture','type':'journal-article','title':['Synthetic theory article'],
          'ISSN':['0000-0000'],'published':{'date-parts':[[2025,1,2]]}}
    calls=[]

    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'status':'ok','message':{'items':[
            work,work,dict(work,ISSN=['1111-1111']),dict(work,DOI='not-a-doi'),
            'corrupt record',dict(work,title='not a title list')]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=discover([journal],since='2025-01-01',until='2025-12-31',client=client)
    assert len(result['records'])==1 and result['skipped_unverified_records']==4
    row=result['records'][0]
    assert row['doi']=='10.1000/fixture' and row['arxiv_id'] is None
    assert not row['admitted_to_paper_catalog'] and row['theory_screen']=='unreviewed'
    assert row['fulltext_status']=='not_fetched' and len(row['metadata_sha256'])==64
    assert 'from-pub-date:2025-01-01' in calls[0].url.params['filter']


def test_failed_journal_does_not_discard_other_journals_or_hide_partial_collection():
    journals=[Journal(id='j'+str(i),journal='Synthetic',issn=issn,domain='Physics')
              for i,issn in enumerate(['0000-0000','1111-1111'])]

    def handler(request):
        if '0000-0000' in request.url.path:return httpx.Response(429)
        return httpx.Response(200,content=json.dumps({'status':'ok','message':{'items':[]}}).encode())

    waits=[]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=discover(journals,since='2025-01-01',until='2025-12-31',client=client,pause=waits.append)
    assert len(result['failures'])==1 and result['failures'][0]['journal_id']=='j0' and waits==[1.1]
    with pytest.raises(ValueError):
        discover(journals,since='2025-01-01',until='2100-01-01',client=client)
