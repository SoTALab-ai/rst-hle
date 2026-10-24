import httpx
import pytest

from diamond_factory.open_access import openalex_locations, scoap_locations


def test_openalex_checks_doi_and_prefers_published_pdf_without_leaking_key_into_url():
    calls = []
    locations = [{'is_oa': True, 'pdf_url': 'https://example.org/preprint.pdf', 'version': 'submittedVersion'},
                 {'is_oa': True, 'pdf_url': 'https://example.org/final.pdf', 'version': 'publishedVersion'},
                 {'is_oa': True, 'pdf_url': 'https://127.0.0.1/private.pdf', 'version': 'publishedVersion'},
                 {'is_oa': False, 'pdf_url': 'https://example.org/closed.pdf', 'version': 'publishedVersion'}]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'doi':'https://doi.org/10.1000/fixture','locations':locations})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = openalex_locations(client, '10.1000/fixture', api_key='test-only-not-a-key')
        with pytest.raises(ValueError, match='DOI identity mismatch'):
            openalex_locations(client, '10.1000/different')
    assert [r.version for r in result] == ['publishedVersion','submittedVersion']
    assert all('test-only' not in r.index_url for r in result)
    assert calls[0].headers['Authorization'] == 'Bearer test-only-not-a-key'
    assert 'api_key' not in calls[0].url.params


def test_scoap_requires_exact_doi_and_does_not_guess_roles_of_multiple_pdfs():
    base = {'id':17, 'metadata':{'dois':[{'value':'10.1000/fixture'}],
        '_files':[{'filetype':'pdf','file':'https://example.org/paper.pdf'}]}}
    other = {'id':18, 'metadata':{'dois':[{'value':'10.1000/wrong'}],
        '_files':[{'filetype':'pdf','file':'https://example.org/wrong.pdf'}]}}
    multi = {'id':19, 'metadata':{'dois':[{'value':'10.1000/fixture'}],
        '_files':base['metadata']['_files']+[{'filetype':'pdf','file':'https://example.org/other.pdf'}]}}
    def handler(request):
        assert request.url.params['search'] == 'doi:"10.1000/fixture"'
        assert 'q' not in request.url.params
        return httpx.Response(200, json={'hits':{'hits':[other,multi,base]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = scoap_locations(client, '10.1000/fixture')
    assert len(result) == 1 and result[0].index_url.endswith('/17')
    assert result[0].version == 'publishedVersion'
