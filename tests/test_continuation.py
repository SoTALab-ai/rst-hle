import asyncio
import json

import pytest
from test_paper_ingest import pg as pg_fixture
from test_paper_ingest import rates
from test_qc import setup_pipeline

from diamond_factory.continuation import ContinuationGateway
from diamond_factory.costs import measure_cost
from diamond_factory.llm import CacheIntegrityError
from diamond_factory.models import AuthoringSeed, Judgment, Trial, digest
from diamond_factory.pipeline import AuthoringRejected
from diamond_factory.verifier import VerifierBuilder


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def answered_gateway(tmp_path, settings, candidate, monkeypatch, store=None):
    for role in ('generator', 'auditor', 'consistency', 'solver', 'judge'):
        config = getattr(settings, role)
        monkeypatch.setenv(config.base_url_env, 'https://offline.invalid/v1')
        monkeypatch.setenv(config.api_key_env, 'synthetic-key')
    gateway = ContinuationGateway(settings, tmp_path/'requests', store, 'continued', pricing=rates(),
        visible_cost_limit_usd=None, historical_candidates=[], historical_traces={})

    def trace(role, prompt, salt, content, schema=None):
        config, payload, rid, _base, _key = gateway._prepare(role, prompt, salt, schema)
        t = {'role': role, 'request': payload, 'request_id': rid, 'salt': salt,
            'actual_model': config.model, 'content': content, 'response_error': None, 'attempts': [], 'usage': {}}
        t['integrity_sha256'] = digest(t)
        return t

    solver = trace('solver', candidate.item.question, 'original:solver:0', '36')
    judge = trace('judge', 'Synthetic unchanged judge prompt', 'original:judge:0', Judgment(
        extracted_final_answer='36', reasoning='A synthetic exact answer match.', correct='yes',
        confidence=100, strict=True).model_dump_json(), Judgment)
    trial = Trial(sample_id=0, response='36', solver_request=solver['request_id'], judge_request=judge['request_id'],
        solver_actual_model=settings.solver.model, judge_actual_model=settings.judge.model,
        judgment=Judgment.model_validate_json(judge['content']))
    original = candidate.model_copy(update={'trials': [trial]})
    gateway = ContinuationGateway(settings, tmp_path/'requests', store, 'continued', pricing=rates(),
        visible_cost_limit_usd=None, historical_candidates=[original],
        historical_traces={solver['request_id']: solver, judge['request_id']: judge})
    return gateway, solver, judge


def test_completed_answer_and_matching_judge_return_original_ids_without_http(tmp_path, settings, candidate, monkeypatch):
    gateway, solver, judge = answered_gateway(tmp_path, settings, candidate, monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError('Already paid response was purchased again')

    gateway._fetch = forbidden
    response = asyncio.run(gateway.call('solver', candidate.item.question, 'new:solver:0'))
    graded = asyncio.run(gateway.call('judge', 'Synthetic unchanged judge prompt', 'new:judge:0', Judgment))
    assert response[1] == solver['request_id'] and graded[1] == judge['request_id']
    assert not list((tmp_path/'requests').glob('*.json'))


@pytest.mark.parametrize('defect', ['payload', 'seal'])
def test_completed_answer_protocol_or_receipt_corruption_refuses_rebuy(
        tmp_path, settings, candidate, monkeypatch, defect):
    gateway, solver, _judge = answered_gateway(tmp_path, settings, candidate, monkeypatch)
    if defect == 'payload':
        settings.solver.temperature = 0.12345
    else:
        solver['content'] = 'changed saved answer'
    with pytest.raises(CacheIntegrityError):
        asyncio.run(gateway.call('solver', candidate.item.question, 'new:solver:0'))
    assert not list((tmp_path/'requests').glob('*.json'))


def test_changed_question_can_buy_only_its_missing_sample(tmp_path, settings, candidate, monkeypatch):
    gateway, _solver, _judge = answered_gateway(tmp_path, settings, candidate, monkeypatch)
    fetched = []

    async def fetch(role, config, payload, request_id, base, key):
        fetched.append(payload)
        return {'actual_model': config.model, 'content': 'new response', 'usage': {}}

    gateway._fetch = fetch
    answer = asyncio.run(gateway.call('solver', candidate.item.question+' A new condition.', 'new:solver:0'))
    assert answer[0] == 'new response' and len(fetched) == 1


def test_changed_gold_reuses_solver_but_rejudges_instead_of_reusing_old_grade(
        tmp_path, settings, candidate, monkeypatch):
    gateway, solver, judge = answered_gateway(tmp_path, settings, candidate, monkeypatch)
    fetched = []

    async def fetch(role, config, *args):
        fetched.append(role)
        return {'actual_model': config.model, 'content': judge['content'], 'usage': {}}

    gateway._fetch = fetch
    result = asyncio.run(gateway.call('solver', candidate.item.question, 'new:solver:0'))
    graded = asyncio.run(gateway.call('judge', 'Changed golden or grading context', 'new:judge:0', Judgment))
    assert result[1] == solver['request_id'] and graded[1] != judge['request_id'] and fetched == ['judge']


def test_imported_proof_is_reaudited_before_question_or_solver(tmp_path, settings, source, candidate):
    from test_bottleneck import enable, prepare
    from test_difficulty import step_review

    from diamond_factory.models import StepwiseBlueprintAudit

    enable(settings); prepare(candidate, source)
    settings.max_repairs = 0
    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    seed = AuthoringSeed(source_id=source.id, known_result=candidate.known_result,
        solution=candidate.solution, item=candidate.item, original_candidate_id='original')

    async def reject(role, prompt, salt, schema=None, **kwargs):
        gateway.calls.append(schema)
        assert schema is StepwiseBlueprintAudit
        return step_review(passed=False), 'independent-review', 'fixture'

    gateway.call = reject
    with pytest.raises(AuthoringRejected):
        asyncio.run(pipe.generate(source, None, {'category': candidate.item.category,
            'subtopic': candidate.item.subtopic}, 0, 0, seed=seed))
    assert gateway.calls == [StepwiseBlueprintAudit]


def test_verifier_draft_seed_stays_untrusted_and_is_not_bought_again(tmp_path, settings, source, candidate):
    from test_provenance_replay import spec

    pipe, _store, gateway = setup_pipeline(tmp_path, settings, source, candidate)
    seed = AuthoringSeed(source_id=source.id, solution=candidate.solution, item=candidate.item,
        verifier_spec=spec(candidate.item, '37 vertices'), verifier_request='old-verifier', verifier_actual_model='fixture')
    candidate.origin_seed_sha256 = digest(seed.model_dump())
    pipe.authoring_seeds[candidate.origin_seed_sha256] = seed
    version = asyncio.run(VerifierBuilder(pipe).draft(candidate, {source.id: source}, 0))
    assert version.spec_request == 'old-verifier' and version.result is None and not gateway.calls


def test_pg_reused_response_cost_and_ownership_remain_on_original_run(
        tmp_path, settings, candidate, monkeypatch, pg):
    store = pg
    for run in ('original-cost', 'continued'):
        store.run(run, {'mode': 'test', 'status': 'running', 'fingerprint': 'fixture'})
    gateway, solver, judge = answered_gateway(tmp_path, settings, candidate, monkeypatch, store)
    solver['attempts'] = [{'request_id': solver['request_id'], 'ordinal': 1, 'run_id': 'original-cost',
        'role': 'solver', 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}]
    solver['integrity_sha256'] = digest({k: v for k, v in solver.items() if k != 'integrity_sha256'})
    for trace in (solver, judge):
        store.save_request('original-cost', trace)
    asyncio.run(gateway.call('solver', candidate.item.question, 'new:solver:0'))
    assert measure_cost(store, 'continued', tmp_path/'requests', settings, rates())['known_cost_usd'] == 0
    assert measure_cost(store, 'original-cost', tmp_path/'requests', settings, rates())['known_cost_usd'] > 0
    assert store.db.execute('SELECT run_id FROM diamond.model_requests WHERE id=%s',
        (solver['request_id'],)).fetchone()[0] == 'original-cost'


def test_pg_prepare_reuses_plan_and_untrusted_proof_without_any_model_call(
        pg, tmp_path, source, candidate, settings, monkeypatch):
    import hashlib
    from pathlib import Path

    from test_bottleneck import prepare
    from test_library_questions import line, paper
    from test_paper_ingest import make_pdf

    from diamond_factory.continuation import old_root_identity, prepare_continuation
    from diamond_factory.experiment import RootPlan
    from diamond_factory.library_questions import MaterialMilestone, PaperOutline, source_for_line
    from diamond_factory.models import ResultSelection

    evidence = tmp_path/'evidence'; (evidence/'pdf').mkdir(parents=True)
    pdf = evidence/'pdf/original.pdf'; make_pdf(pdf)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest();pdf.rename(evidence/'pdf'/f'{sha}.pdf')
    material = paper();material.documents[0].pdf_sha256 = sha
    proposal = line();derived = source_for_line(material, proposal, 10000)
    for ev in candidate.item.evidence+candidate.solution.evidence:
        ev.source_id = derived.id
    candidate.source_ids = [derived.id]; prepare(candidate, derived)
    origin = 'frozen-origin'
    root = RootPlan(name='L50_001_q1', source_id=derived.id, category=proposal.category,
        subtopic=proposal.subtopic, focus='An original frozen independent target with an already paid plan.')
    identity = old_root_identity(root, origin)
    candidate.id = candidate.root_id = identity;candidate.run_id = origin
    outline = PaperOutline(outcome='ready', lines=[proposal], reason='One frozen synthetic construction for replay.')
    payloads = {'sources': [derived.model_dump()], 'candidates': [candidate.model_dump()],
        'milestone': MaterialMilestone(library_run_id='library', required_materials=1, papers=[material]).model_dump(),
        'experiment': {'roots': []}, 'planning-manifest': {'settings': {'max_source_chars': 400000}},
        'planning': {'run_id': 'old-planning', 'papers': [{'paper_id': material.id, 'doi': material.doi,
            'status': 'planned', 'outline': outline.model_dump(), 'planning_request': 'paid-plan',
            'roots': [root.model_dump()]}]}}
    def write_trace(rid, salt, data):
        path = evidence/'traces'/f'{rid}.json';path.parent.mkdir(exist_ok=True)
        trace = {'request_id': rid, 'salt': salt, 'role': 'generator', 'response_error': None,
                 'actual_model': settings.generator.model, 'content': json.dumps(data), 'request': {}, 'attempts': [], 'usage': {}}
        trace['integrity_sha256'] = digest(trace);path.write_text(json.dumps(trace))
    write_trace('paid-plan', 'paper:plan', outline.model_dump())
    write_trace('paid-result', identity+':known-result:0', ResultSelection(outcome='found',
        result=candidate.known_result, reason='A frozen synthetic proposed result.').model_dump())
    write_trace('paid-proof', identity+':repair:0:solution', candidate.solution.model_dump())
    for name, data in payloads.items(): (evidence/(name+'.json')).write_text(json.dumps(data))
    files = {str(p.relative_to(evidence)): hashlib.sha256(p.read_bytes()).hexdigest() for p in evidence.rglob('*') if p.is_file()}
    manifest = evidence/'manifest.json';manifest.write_text(json.dumps({'origin_run': origin, 'files': files}))
    seal = hashlib.sha256(manifest.read_bytes()).hexdigest()
    settings.source_fidelity = 'advisory'

    async def forbidden(*args, **kwargs):
        raise AssertionError('Frozen plan or proof was purchased again')
    monkeypatch.setattr('diamond_factory.continuation.MeteredGateway.call', forbidden)
    plan, report, _candidates, _traces = asyncio.run(prepare_continuation(pg, settings, rates(), evidence,
        seal, tmp_path/'continued', 'continue'))
    assert len(plan.roots) == 1 and report['reused_seed_roots'] == 1
    assert plan.authoring_seeds[root.name].original_request_ids == ['paid-result', 'paid-proof']
    assert report['planning_cost']['known_cost_usd'] == 0 and report['planning_cost']['http_attempts'] == 0
    assert pg.db.execute('SELECT run_id FROM diamond.model_requests WHERE id=%s', ('paid-proof',)).fetchone()[0] == origin
    assert not (Path(tmp_path)/'continued/questions').exists()
