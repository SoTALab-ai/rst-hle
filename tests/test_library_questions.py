import asyncio
import json

import pytest
from pydantic import ValidationError
from test_paper_ingest import make_pdf, rates, readable
from test_paper_ingest import pg as pg_fixture

from diamond_factory.library_questions import (
    MaterialMilestone,
    PaperOutline,
    QuestionLine,
    SnapshotDocument,
    SnapshotPaper,
    admit_lines,
    complete_paper_ids,
    freeze_milestone,
    plan_questions,
    source_for_line,
)
from diamond_factory.models import digest
from diamond_factory.paper_ingest import LibraryRepository, ReadBatch, inventory_directory


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def paper():
    return SnapshotPaper(id='paper-a', title='Synthetic independent result fixture', doi='10.1000/fixture',
        journal='Synthetic test journal', domain='Math', supplement_status='not_checked', documents=[
            SnapshotDocument(ordinal=0, role='main', pdf_sha256='a'*64, parse_id='parse-a',
                pages=[readable(1), readable(2)], reader_requests=['reader-a'])])


def line(i=1):
    return QuestionLine(id=f'q{i}', category='Math', subtopic='组合与图论', result_label=f'Theorem {i}',
        target=f'An existing synthetic result target {i}, not a scientific paper claim.',
        provisional_answer='36', bottleneck=f'Independently reconstruct the necessary synthetic argument {i}.',
        structural_delta=f'This is a different synthetic inference structure for fixture {i}.',
        source_quote=readable().text, pages=[{'document': 0, 'page': 1}])


def test_one_to_five_lines_and_source_coverage_contract():
    outline = PaperOutline(outcome='ready', lines=[line(i) for i in range(1, 6)], reason='Synthetic bounded plan fixture')
    assert len(outline.lines) == 5
    with pytest.raises(ValidationError):
        PaperOutline(outcome='ready', lines=[], reason='No line cannot be called ready')
    with pytest.raises(ValidationError, match='Duplicate'):
        PaperOutline(outcome='ready', lines=[line(), line()], reason='Duplicate lines must be rejected')
    assert 'physical page 1' in source_for_line(paper(), line(), 10000).text
    bad = line().model_copy(update={'source_quote': 'invented citation that never appeared'})
    with pytest.raises(ValueError, match='anchor'):
        source_for_line(paper(), bad, 10000)
    with pytest.raises(ValueError, match='context'):
        source_for_line(paper(), line(), 20)


def test_aliases_and_per_line_failure_preserve_other_valid_lines():
    good = line().model_copy(update={'category': 'Physics', 'subtopic': '量子場論與粒子物理'})
    bad = line(2).model_copy(update={'source_quote': 'A fabricated source statement not present.'})
    selected, receipts = admit_lines(paper(), [good, bad],
        {('Physics', '量子场论与粒子物理'), ('Math', '组合与图论')}, 10000)
    assert len(selected) == 1 and selected[0][0].subtopic == '量子场论与粒子物理'
    assert good.subtopic == '量子場論與粒子物理'  # 原模型响应不改写。
    assert receipts[0]['status'] == 'admitted' and receipts[1]['status'] == 'rejected'
    assert not any(r['scientific_pass'] for r in receipts)
    unsupported = good.model_copy(update={'subtopic': '凝聚态物理'})
    assert not admit_lines(paper(), [unsupported], {('Physics', '量子场论与粒子物理')}, 10000)[0]


def setup_registered(pg, tmp_path, name, count=2):
    pg.run(name, {'mode': 'pdf_library', 'status': 'running', 'fingerprint': name})
    folder = tmp_path/name
    folder.mkdir()
    make_pdf(folder/'paper.pdf')
    material = inventory_directory(folder, 1).papers[0].model_copy(update={
        'domain': 'Math', 'title': f'Synthetic material {name}', 'doi': f'10.1000/{name}'})
    repo = LibraryRepository(pg)
    pid = repo.register(name, material, {'reader': name})
    parse = repo.documents(name)[0][0]
    return repo, pid, parse


def save_pages(pg, repo, parse, run, numbers):
    batch = ReadBatch(pages=[readable(n) for n in numbers])
    request_id = digest({'run': run, 'pages': numbers})
    pg.save_request(run, {'request_id': request_id, 'role': 'generator', 'actual_model': 'gpt-6-luna',
                         'usage': {}, 'content': batch.model_dump_json()})
    repo.save_batch(parse, batch, request_id, 'gpt-6-luna')


@pytest.mark.usefixtures('pg')
def test_pg_registered_and_partial_do_not_trigger_and_frozen_selection_is_stable(pg, tmp_path):
    repo, pid, parse = setup_registered(pg, tmp_path, 'library')
    path = tmp_path/'milestone.json'
    assert complete_paper_ids(pg, 'library') == []
    assert freeze_milestone(pg, 'library', 1, path) is None and not path.exists()
    save_pages(pg, repo, parse, 'library', [1])
    assert complete_paper_ids(pg, 'library') == []
    save_pages(pg, repo, parse, 'library', [2])
    selected = freeze_milestone(pg, 'library', 1, path)
    assert selected.papers[0].id == pid
    assert freeze_milestone(pg, 'library', 1, path) == selected
    with pytest.raises(ValueError, match='Frozen'):
        freeze_milestone(pg, 'library', 2, path)
    row = pg.db.execute('SELECT object_key FROM diamond.library_pages WHERE parse_id=%s LIMIT 1', (parse,)).fetchone()
    (pg.objects.root/row[0]).write_text('corrupted')
    with pytest.raises(ValueError, match='corrupt'):
        freeze_milestone(pg, 'library', 1, path)


def test_pg_planning_rejection_never_calls_solver(pg, tmp_path, settings, monkeypatch):
    import httpx
    from test_gateway import install_mock

    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload['model'])
        content = PaperOutline(outcome='unavailable', lines=[], reason='No independent difficult result in this synthetic fixture.').model_dump_json()
        return httpx.Response(200, json={'model': settings.generator.model, 'choices': [
            {'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})

    install_mock(monkeypatch, handler)
    settings = settings.model_copy(update={'request_cap_enabled': False})
    milestone = MaterialMilestone(library_run_id='library', required_materials=1, papers=[paper()])
    plans, report = asyncio.run(plan_questions(pg, milestone, settings, rates(), tmp_path/'planning', 'plan'))
    assert plans == [] and report['unavailable_or_rejected_materials'] == 1
    assert calls == [settings.generator.model] and pg.candidates() == []
    assert asyncio.run(plan_questions(pg, milestone, settings, rates(), tmp_path/'planning', 'plan'))[0] == []
    assert calls == [settings.generator.model]  # 已付费规划响应复用。


def test_pg_unreadable_or_empty_paper_does_not_count_as_ready(pg, tmp_path):
    from diamond_factory.paper_ingest import ReadPage

    repo, _pid, parse = setup_registered(pg, tmp_path, 'unclear')
    batch = ReadBatch(pages=[readable(1), ReadPage(page=2, status='unreadable', text='partial',
                                               formula_labels=[], unreadable=['Eq.5 operator unclear'])])
    pg.save_request('unclear', {'request_id': 'unclear-response', 'role': 'generator',
        'actual_model': 'gpt-6-luna', 'usage': {}, 'content': batch.model_dump_json()})
    repo.save_batch(parse, batch, 'unclear-response', 'gpt-6-luna')
    assert complete_paper_ids(pg, 'unclear') == []


def test_pg_planning_independent_review_keeps_only_distinct_roots(pg, tmp_path, settings, monkeypatch):
    import httpx
    from test_gateway import install_mock

    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload['model'])
        content = (PaperOutline(outcome='ready', lines=[line(1), line(2)],
            reason='Two proposed synthetic targets require independent review.').model_dump_json()
            if len(calls) == 1 else json.dumps({'keep_ids': ['q1'],
                'reason': 'The second proposal repeats the same inference despite different labels.'}))
        return httpx.Response(200, json={'model': payload['model'], 'choices': [
            {'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})

    install_mock(monkeypatch, handler)
    settings = settings.model_copy(update={'request_cap_enabled': False})
    milestone = MaterialMilestone(library_run_id='library', required_materials=1, papers=[paper()])
    plans, report = asyncio.run(plan_questions(pg, milestone, settings, rates(), tmp_path/'planning', 'plan'))
    assert len(plans) == 1 and len(plans[0].roots) == 1
    assert report['planned_independent_roots'] == 1 and len(pg.sources()) == 1
    assert calls == [settings.generator.model, settings.auditor.model]
    assert pg.candidates() == []  # 规划通过不构造科学审查通过的候选。


def test_pg_invalid_sibling_does_not_drop_valid_roots_or_skip_review(pg, tmp_path, settings, monkeypatch):
    import httpx
    from test_gateway import install_mock

    calls = []
    bad = line(3).model_copy(update={'source_quote': 'Unsupported quotation that must still fail.'})

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        content = (PaperOutline(outcome='ready', lines=[line(1), bad, line(2)],
            reason='One bad anchor must not discard two unrelated valid lines.').model_dump_json()
            if len(calls) == 1 else json.dumps({'keep_ids': ['q1', 'q2'],
                'reason': 'These two synthetic lines have different required inference structures.'}))
        return httpx.Response(200, json={'model': payload['model'], 'choices': [
            {'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})

    install_mock(monkeypatch, handler)
    settings = settings.model_copy(update={'request_cap_enabled': False})
    milestone = MaterialMilestone(library_run_id='library', required_materials=1, papers=[paper()])
    plans, report = asyncio.run(plan_questions(pg, milestone, settings, rates(), tmp_path/'planning', 'plan'))
    assert len(plans[0].roots) == 2 and len(pg.sources()) == 2 and not pg.candidates()
    assert [r['status'] for r in report['papers'][0]['line_admission']] == ['admitted', 'rejected', 'admitted']
    assert len(calls) == 2 and calls[1]['model'] == settings.auditor.model
    assert 'Unsupported quotation' not in json.dumps(calls[1])


@pytest.mark.parametrize('config', ['library-source-inspired', 'library-paper-hard-step'])
def test_pg_advisory_planning_keeps_nonliteral_inspiration_as_note(pg, tmp_path, monkeypatch, config):
    import httpx
    from test_gateway import install_mock

    from diamond_factory.config import load

    settings = load('configs/' + config + '.toml')
    proposal = line().model_copy(update={'source_quote': 'A self-contained constructed variant inspired by the source.'})
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert 'source_fidelity' in payload['messages'][-1]['content']
        if settings.scientific_goal_references_path:
            prompt = payload['messages'][-1]['content']
            assert 'defer answer form and candidate answers' in prompt
            assert 'then their natural answer forms and candidate answers' not in prompt
        return httpx.Response(200, json={'model': payload['model'], 'choices': [{'message': {'content':
            PaperOutline(outcome='ready', lines=[proposal],
                reason='A hypothetical construction will undergo full independent proof review.').model_dump_json()},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})

    install_mock(monkeypatch, handler)
    milestone = MaterialMilestone(library_run_id='library', required_materials=1, papers=[paper()])
    plans, report = asyncio.run(plan_questions(pg, milestone, settings, rates(), tmp_path/'planning', 'plan'))
    assert len(plans[0].roots) == 1 and len(calls) == 1
    assert report['papers'][0]['line_admission'][0]['source_notes']
    focus = plans[0].roots[0].focus
    assert not pg.candidates()
    if settings.scientific_goal_references_path:
        assert focus.startswith('First select a scientific goal')
        assert 'faithful paper citations' in focus
        assert 'independently prove it' not in focus
    else:
        assert 'INSPIRATION' in focus


def test_material_versions_cannot_inflate_trigger_count():
    first = paper()
    revision = first.model_copy(update={"id": "different-metadata-version", "doi": "10.1000/FIXTURE"})
    with pytest.raises(ValidationError, match="distinct materials"):
        MaterialMilestone(library_run_id="library", required_materials=2, papers=[first, revision])


def test_excluded_cohort_selects_new_materials_and_is_frozen(tmp_path, monkeypatch):
    import diamond_factory.library_questions as module

    fresh = paper().model_copy(deep=True)
    fresh.id = 'fresh-paper'
    fresh.doi = '10.1000/fresh'
    fresh.documents[0].pdf_sha256 = 'b'*64
    monkeypatch.setattr(module, 'complete_paper_ids', lambda *args: ['already-used','fresh-paper'])
    monkeypatch.setattr(module, 'snapshot_paper', lambda store, run, pid: fresh)
    path = tmp_path/'remaining.json'
    selected = freeze_milestone(None, 'library', 1, path, exclude=['already-used'])
    assert [p.id for p in selected.papers] == ['fresh-paper']
    assert selected.excluded_paper_ids == ['already-used']
    assert freeze_milestone(None, 'library', 1, path, exclude=['already-used']) == selected
    with pytest.raises(ValueError, match='Frozen'):
        freeze_milestone(None, 'library', 1, path, exclude=[])
    with pytest.raises(ValidationError, match='disjoint'):
        MaterialMilestone(library_run_id='library',required_materials=1,papers=[fresh],
                          excluded_paper_ids=['fresh-paper'])


def test_pg_exclusion_does_not_treat_registered_material_as_another_complete_paper(pg, tmp_path):
    repo, pid, parse = setup_registered(pg, tmp_path, 'exclude-library')
    save_pages(pg, repo, parse, 'exclude-library', [1,2])
    path = tmp_path/'remaining.json'
    assert freeze_milestone(pg, 'exclude-library', 1, path, exclude=[pid]) is None
    assert not path.exists()
    assert freeze_milestone(pg, 'exclude-library', 1, path).papers[0].id == pid
