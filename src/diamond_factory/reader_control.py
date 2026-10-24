"""对冻结的旧读取器添加独立预约门禁，排空在途收据后停止；不热改模型代码。"""
import hashlib
import json
import os
import signal
import time
from pathlib import Path

from psycopg.types.json import Jsonb
from pydantic import Field

from .library_questions import complete_paper_ids, freeze_milestone
from .models import StrictModel, digest
from .runtime import atomic_json


class ReaderLimit(StrictModel):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    target_run_id: str
    material_limit: int = Field(ge=1, le=200)
    worker_pid: int = Field(gt=1)
    worker_command_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot: str
    requests_directory: str
    reason: str = Field(min_length=10)


def install_control(store):
    """必须显式执行init；控制面迁移与旧运行的diamond迁移链隔离。"""
    path = Path(__file__).resolve().parents[2]/'deployment/control/001_reader_limit.sql'
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    with store.db.transaction():
        store.db.execute('SELECT pg_advisory_xact_lock(704693178)')
        store.db.execute('CREATE SCHEMA IF NOT EXISTS rst_hle_control')
        store.db.execute('CREATE TABLE IF NOT EXISTS rst_hle_control.schema_migrations '
                         '(version integer PRIMARY KEY,checksum text NOT NULL)')
        prior = store.db.execute('SELECT checksum FROM rst_hle_control.schema_migrations WHERE version=1').fetchone()
        if prior:
            if prior[0] != checksum:
                raise ValueError('Control migration checksum mismatch')
            return
        store.db.execute(path.read_text())
        store.db.execute('INSERT INTO rst_hle_control.schema_migrations VALUES(1,%s)', (checksum,))


def register_limit(store, policy, directory):
    target = store.runs().get(policy.target_run_id)
    if not target or target['mode'] != 'pdf_library':
        raise ValueError('Only an existing PDF reading run can be limited')
    manifest = {'policy': policy.model_dump(), 'target_fingerprint': target['fingerprint'],
        'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    seal = digest(manifest)
    prior = store.db.execute('SELECT manifest_sha256 FROM rst_hle_control.reader_limits WHERE id=%s',
                             (policy.id,)).fetchone()
    if prior and prior[0] != seal:
        raise ValueError('Frozen control changed; use a new control identity')
    path = Path(directory)/'manifest.json'
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError('Frozen control directory changed')
    atomic_json(path, manifest)
    store.db.execute('INSERT INTO rst_hle_control.reader_limits '
        '(id,target_run_id,material_limit,manifest_sha256,manifest) VALUES(%s,%s,%s,%s,%s) '
        'ON CONFLICT(id) DO NOTHING', (policy.id, policy.target_run_id, policy.material_limit, seal, Jsonb(manifest)))


def drain_evidence(store, policy):
    """HTTP尝试均有落盘收据，200响应另须逻辑响应持久化，才可终止旧进程。"""
    row = store.db.execute('SELECT http_attempts FROM diamond.runs WHERE id=%s', (policy.target_run_id,)).fetchone()
    reserved = row[0]
    receipts = {}
    for p in (Path(policy.requests_directory)/'attempts').rglob('*.json'):
        body = json.loads(p.read_text())
        if body.get('run_id') != policy.target_run_id:
            continue
        ordinal = body['ordinal']
        if ordinal in receipts and receipts[ordinal] != body:
            raise ValueError('Conflicting HTTP reservation receipts')
        receipts[ordinal] = body
    missing = sorted(set(range(1, reserved+1))-set(receipts))
    ids = {r[0] for r in store.db.execute('SELECT id FROM diamond.model_requests')}
    unindexed = [r['request_id'] for r in receipts.values() if r.get('status_code') == 200 and r['request_id'] not in ids]
    return {'reserved': reserved, 'received_attempts': len(receipts), 'missing_ordinals': missing,
            'successful_responses_not_durable': unindexed, 'drained': not missing and not unindexed}, receipts


def stop_owned_worker(policy):
    proc = Path('/proc')/str(policy.worker_pid)
    if not proc.exists():
        return
    raw = (proc/'cmdline').read_bytes()
    if (hashlib.sha256(raw).hexdigest() != policy.worker_command_sha256
            or str((proc/'cwd').resolve()) != policy.snapshot or os.getpgid(policy.worker_pid) != policy.worker_pid):
        raise ValueError('Worker identity changed; refusing to signal it')
    os.killpg(policy.worker_pid, signal.SIGTERM)


def supervise_limit(store, policy, directory):
    directory = Path(directory)
    register_limit(store, policy, directory)
    last = None
    while True:
        state = store.db.execute('SELECT state,denied_reservations FROM rst_hle_control.reader_limits WHERE id=%s',
                                 (policy.id,)).fetchone()
        ready = len(complete_paper_ids(store, policy.target_run_id, 200))
        if state[0] == 'watching' and ready >= policy.material_limit:
            store.db.execute("UPDATE rst_hle_control.reader_limits SET state='draining', "
                             "blocked_at=coalesce(blocked_at,now()) WHERE id=%s", (policy.id,))
            state = ('draining', state[1])
        current = (state[0], ready)
        if current != last:
            print(json.dumps({'control': policy.id, 'state': state[0], 'complete_materials': ready,
                              'limit': policy.material_limit}), flush=True)
            last = current
        if state[0] in ('draining', 'stopped'):
            evidence, receipts = drain_evidence(store, policy)
            if evidence['drained']:
                break
        if not (Path('/proc')/str(policy.worker_pid)).exists():
            raise RuntimeError('Reader ended before the controlled drain completed')
        time.sleep(1)
    # 门禁已阻止新HTTP，成功响应也均已落盘，允许结束尚未发请求的排队渲染。
    stop_owned_worker(policy)
    for _ in range(100):
        try:
            with store.lock():
                for attempt in receipts.values():
                    store.save_attempt(policy.target_run_id, attempt)
                original = store.runs()[policy.target_run_id]
                store.run(policy.target_run_id, {**original, 'status': 'stopped_at_material_limit'})
                store.event(policy.target_run_id, {'event': 'stopped_at_material_limit',
                    'control_id': policy.id, 'limit': policy.material_limit, 'cancelled_inflight_calls': 0})
                break
        except RuntimeError:
            time.sleep(.1)
    else:
        raise RuntimeError('Old writer did not release its lock after draining')
    store.db.execute("UPDATE rst_hle_control.reader_limits SET state='stopped' WHERE id=%s", (policy.id,))
    ids = complete_paper_ids(store, policy.target_run_id, 200)
    milestone = freeze_milestone(store, policy.target_run_id, policy.material_limit, directory/'materials.json')
    denied = store.db.execute('SELECT denied_reservations FROM rst_hle_control.reader_limits WHERE id=%s',
                              (policy.id,)).fetchone()[0]
    report = {'status': 'stopped_at_material_limit', 'target_run_id': policy.target_run_id,
        'material_limit': policy.material_limit, 'retained_complete_materials': len(milestone.papers),
        'actual_complete_materials': len(ids), 'extra_from_already_inflight': max(0, len(ids)-policy.material_limit),
        'denied_reservations': denied, 'cancelled_inflight_calls': 0, 'drain_evidence': evidence,
        'note': '登记过的其余论文不是已解析成功；停止后的BudgetExceeded为未获预约，不是模型错误或科学失败。'}
    atomic_json(directory/'report.json', report)
    return report
