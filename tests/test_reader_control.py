import hashlib
import json
from pathlib import Path

import pytest
from test_library_questions import save_pages, setup_registered
from test_paper_ingest import pg as pg_fixture

from diamond_factory.reader_control import ReaderLimit, drain_evidence, install_control, register_limit


@pytest.fixture
def pg(tmp_path):
    yield from pg_fixture.__wrapped__(tmp_path)


def policy(tmp_path, target='reader'):
    return ReaderLimit(id='stop-reader-v1', target_run_id=target, material_limit=1, worker_pid=12345,
        worker_command_sha256='a'*64, snapshot='/controlled/snapshot', requests_directory=str(tmp_path/'requests'),
        reason='Controlled synthetic user material limit change')


def test_explicit_gate_blocks_only_target_new_http_and_is_idempotent(pg, tmp_path):
    install_control(pg)
    install_control(pg)
    repo, _paper, parse = setup_registered(pg, tmp_path, 'reader')
    register_limit(pg, policy(tmp_path), tmp_path/'control')
    assert pg.reserve_request('reader', None) == 1
    save_pages(pg, repo, parse, 'reader', [1, 2])
    assert pg.reserve_request('reader', None) is None
    assert pg.db.execute("SELECT http_attempts FROM diamond.runs WHERE id='reader'").fetchone()[0] == 1
    assert pg.db.execute("SELECT state FROM rst_hle_control.reader_limits WHERE id='stop-reader-v1'").fetchone()[0] == 'draining'
    pg.run('question', {'mode': 'pilot', 'status': 'running', 'fingerprint': 'test'})
    assert pg.reserve_request('question', None) == 1
    # 控制面不写旧diamond迁移版本，老冻结程序仍能校验自己的迁移链。
    assert pg.db.execute('SELECT count(*) FROM diamond.schema_migrations').fetchone()[0] == 5
    pg.check_schema()
    with pytest.raises(ValueError, match='Frozen control'):
        register_limit(pg, policy(tmp_path).model_copy(update={'material_limit': 2}), tmp_path/'control')


def test_drain_refuses_pending_http_and_unpersisted_success_but_retains_failed_receipts(pg, tmp_path):
    pg.run('reader', {'mode': 'pdf_library', 'status': 'running', 'fingerprint': 'reader'})
    pg.reserve_request('reader', None)
    p = policy(tmp_path)
    evidence, _ = drain_evidence(pg, p)
    assert not evidence['drained'] and evidence['missing_ordinals'] == [1]
    folder = Path(p.requests_directory)/'attempts/request'
    folder.mkdir(parents=True)
    attempt = {'run_id': 'reader', 'request_id': 'request', 'ordinal': 1, 'role': 'generator',
               'status_code': 200, 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
    (folder/'1.json').write_text(json.dumps(attempt))
    evidence, _ = drain_evidence(pg, p)
    assert not evidence['drained'] and evidence['successful_responses_not_durable'] == ['request']
    pg.save_request('reader', {'request_id': 'request', 'actual_model': 'gpt-6-luna', 'role': 'generator',
                            'usage': attempt['usage'], 'content': 'Controlled successful response'})
    assert drain_evidence(pg, p)[0]['drained']
    pg.reserve_request('reader', None)
    (folder/'2.json').write_text(json.dumps({**attempt, 'request_id': 'failed', 'ordinal': 2, 'status_code': 503, 'usage': {}}))
    evidence, receipts = drain_evidence(pg, p)
    assert evidence['drained'] and len(receipts) == 2


def test_control_migration_tampering_is_refused(pg):
    install_control(pg)
    checksum = hashlib.sha256(b'changed').hexdigest()
    pg.db.execute('UPDATE rst_hle_control.schema_migrations SET checksum=%s WHERE version=1', (checksum,))
    try:
        with pytest.raises(ValueError, match='checksum'):
            install_control(pg)
    finally:
        path = Path('deployment/control/001_reader_limit.sql')
        pg.db.execute('UPDATE rst_hle_control.schema_migrations SET checksum=%s WHERE version=1',
                      (hashlib.sha256(path.read_bytes()).hexdigest(),))


def test_supervisor_waits_for_durable_success_before_signalling(pg, tmp_path, monkeypatch):
    import diamond_factory.reader_control as control

    install_control(pg)
    repo, _paper, parse = setup_registered(pg, tmp_path, 'reader')
    pg.reserve_request('reader', None)
    save_pages(pg, repo, parse, 'reader', [1, 2])
    request_id = pg.db.execute("SELECT id FROM diamond.model_requests WHERE run_id='reader'").fetchone()[0]
    p = policy(tmp_path)
    fake_proc = tmp_path/'proc'
    (fake_proc/str(p.worker_pid)).mkdir(parents=True)
    real_path = Path
    monkeypatch.setattr(control, 'Path', lambda value: fake_proc if value == '/proc' else real_path(value))
    stopped = []

    def finish_pending(_seconds):
        assert not stopped
        folder = Path(p.requests_directory)/'attempts'/request_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'1.json').write_text(json.dumps({'run_id': 'reader', 'request_id': request_id,
            'ordinal': 1, 'role': 'generator', 'status_code': 200, 'usage': {}}))

    monkeypatch.setattr(control.time, 'sleep', finish_pending)
    monkeypatch.setattr(control, 'stop_owned_worker', lambda _: stopped.append(drain_evidence(pg, p)[0]['drained']))
    report = control.supervise_limit(pg, p, tmp_path/'control')
    assert stopped == [True] and report['cancelled_inflight_calls'] == 0
    assert report['actual_complete_materials'] == report['retained_complete_materials'] == 1
    assert pg.runs()['reader']['status'] == 'stopped_at_material_limit'
