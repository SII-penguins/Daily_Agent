"""Offline synthetic evidence only; these tests do not prove remote persistence."""
from copy import deepcopy
from pathlib import Path
import shutil

import pytest

from daily_agent import durable_boundary as boundary, durable_checkpoint as checkpoint
from daily_agent.cloud_workflow import init_profile
from daily_agent.state_archive import snapshot, restore
from daily_agent.workflow_state import StateCorrupt, read_json


@pytest.fixture
def batch(tmp_path):
    root = tmp_path/'state'
    init_profile(Path(__file__).resolve().parents[1], root, 'codex')
    previous = snapshot(root, tmp_path/'previous.zip')
    return {'root': root, 'tmp': tmp_path, 'control': tmp_path/'control',
            'parent': {'library_file_id': 'fixture-library', 'file_id': 'fixture-0',
                       'version': 0, 'sha256': previous['sha256'], 'size': previous['size']}}


def committed(batch):
    marker = read_json(batch['root']/boundary.BOUNDARY)
    pending = checkpoint.prepare_checkpoint(batch['root'], batch['control'], parent=batch['parent'],
        purpose='offline boundary test', boundary=marker['token'], owner=marker['owner'])
    upload = checkpoint.begin_upload(batch['control'], pending['manifest']['checkpoint_id'], current=batch['parent'])
    info = pending['manifest']['archive']
    version = batch['parent']['version'] + 1
    current = {'library_file_id': batch['parent']['library_file_id'], 'file_id': f'fixture-{version}',
               'version': version, 'sha256': info['sha256'], 'size': info['size']}
    remote = batch['tmp']/f'materialized-{version}.zip'
    shutil.copyfile(upload['request']['file'], remote)
    write = {'status': 'success', 'operation': 'replace', 'checkpoint_id': upload['checkpoint_id'],
             'attempt_id': upload['attempt_id'], 'request': {key: upload['request'][key]
             for key in ('library_file_id', 'expected_current_version')}, 'result': current}
    receipt = checkpoint.commit_checkpoint(batch['control'], upload['checkpoint_id'],
        write_result=write, current=current, materialized_archive=remote)
    batch['parent'] = current
    return receipt, remote


def ready(batch):
    marker = boundary.arm_boundary(batch['root'], 'generate', 'fixture-owner')
    receipt, remote = committed(batch)
    boundary.verify_boundary_checkpoint(batch['root'], receipt, remote)
    return marker, receipt, remote


def test_full_cycle_and_single_consumption(batch):
    root = batch['root']
    marker, receipt, remote = ready(batch)
    assert boundary.boundary_status(root)['phase'] == 'ready'
    token = boundary.begin_boundary(root, 'generate')
    assert token == marker['token']
    with pytest.raises(StateCorrupt):
        boundary.begin_boundary(root, 'generate')
    boundary.settle_boundary(root, token)
    with pytest.raises(StateCorrupt, match='settlement'):
        boundary.arm_boundary(root, 'generate', 'fixture-owner')
    settled, readback = committed(batch)
    boundary.verify_boundary_checkpoint(root, settled, readback)
    next_marker = boundary.arm_boundary(root, 'deliver', 'fixture-owner')
    assert next_marker['token'] != token
    assert next_marker['parent_library'] == settled['library']


def test_unverified_or_wrong_kind_never_begins(batch):
    root = batch['root']
    boundary.arm_boundary(root, 'generate', 'fixture-owner')
    with pytest.raises(StateCorrupt):
        boundary.begin_boundary(root, 'generate')
    receipt, archive = committed(batch)
    boundary.verify_boundary_checkpoint(root, receipt, archive)
    with pytest.raises(StateCorrupt):
        boundary.begin_boundary(root, 'deliver')
    assert boundary.boundary_status(root)['phase'] == 'ready'


@pytest.mark.parametrize('fault', ['owner', 'token', 'source', 'digest', 'write', 'bytes', 'marker'])
def test_bad_checkpoint_preserves_intent(batch, fault):
    root = batch['root']
    boundary.arm_boundary(root, 'generate', 'fixture-owner')
    receipt, archive = committed(batch)
    receipt = deepcopy(receipt)
    if fault in {'owner', 'token', 'source'}:
        key = {'owner': 'owner', 'token': 'boundary', 'source': 'state_root'}[fault]
        receipt['manifest'][key] = '/other' if fault == 'source' else 'other'
        receipt['manifest_sha256'] = checkpoint._hash(receipt['manifest'])
    elif fault == 'digest':
        receipt['library']['sha256'] = 'f'*64
    elif fault == 'write':
        receipt['write_result']['request']['expected_current_version'] = 8
    elif fault == 'bytes':
        archive.write_bytes(b'corrupt')
    else:
        # A valid receipt for an earlier marker cannot authorize a modified intent.
        marker = read_json(root/boundary.BOUNDARY)
        marker['kind'] = 'deliver'
        (root/boundary.BOUNDARY).write_text(__import__('json').dumps(marker))
    with pytest.raises(StateCorrupt):
        boundary.verify_boundary_checkpoint(root, receipt, archive)
    assert read_json(root/boundary.BOUNDARY)['phase'] == 'intent'


def test_settlement_preserves_budget_and_rejects_active_work(batch):
    root = batch['root']
    ready(batch)
    token = boundary.begin_boundary(root, 'generate')
    budget = root/'data/state/budget.json'
    original = '{"remaining_seconds":0,"active_attempt_id":"unfinished"}'
    budget.write_text(original)
    with pytest.raises(StateCorrupt, match='Active'):
        boundary.settle_boundary(root, token)
    assert budget.read_text() == original
    assert boundary.boundary_status(root)['phase'] == 'executing'
    budget.write_text('{"remaining_seconds":0,"active_attempt_id":null}')
    boundary.settle_boundary(root, token)
    assert read_json(budget)['remaining_seconds'] == 0


def test_loss_after_work_restores_intent_not_fresh_execution(batch):
    root = batch['root']
    marker, receipt, archive = ready(batch)
    boundary.begin_boundary(root, 'generate')
    # Work may already have sent a message or spent budget; no final checkpoint.
    restored = batch['tmp']/'recovered'
    restore(archive, restored, receipt['library']['sha256'])
    assert read_json(restored/boundary.BOUNDARY)['phase'] == 'intent'
    with pytest.raises(StateCorrupt):
        boundary.begin_boundary(restored, 'generate')
    with pytest.raises(StateCorrupt):
        boundary.arm_boundary(restored, 'generate', 'fixture-owner')


def test_wrong_token_and_recovery_fence_block_settlement(batch):
    root = batch['root']
    ready(batch)
    token = boundary.begin_boundary(root, 'generate')
    with pytest.raises(StateCorrupt):
        boundary.settle_boundary(root, '0'*32)
    (root/'.daily-agent-recovery.json').write_text('{}')
    with pytest.raises(StateCorrupt, match='fenced'):
        boundary.settle_boundary(root, token)
    assert read_json(root/boundary.BOUNDARY)['phase'] == 'executing'


def test_settlement_accepts_optional_null_without_changing_it(batch):
    root=batch['root'];ready(batch);token=boundary.begin_boundary(root,'generate')
    path=root/'data/scientific-analysis'/('a'*64)/'writer-repair.json'
    path.parent.mkdir(parents=True);path.write_bytes(b'null\n')
    boundary.settle_boundary(root,token)
    assert boundary.boundary_status(root)['phase']=='settled'
    assert path.read_bytes()==b'null\n'
    receipt,archive=committed(batch)
    boundary.verify_boundary_checkpoint(root,receipt,archive)
    assert path.read_bytes()==b'null\n'


@pytest.mark.parametrize('relative',[
    'data/state/cloud-generation.json',
    'data/state/cloud-generation-budgets/2026-10-10.json',
    'data/state/cloud-delivery/2026-10-10.json',
    'data/state/writer-repair.json',
])
def test_settlement_rejects_required_null_and_preserves_executing(batch,relative):
    root=batch['root'];ready(batch);token=boundary.begin_boundary(root,'generate')
    path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'null\n')
    before=(root/boundary.BOUNDARY).read_bytes()
    with pytest.raises(StateCorrupt,match='Null workflow record'):boundary.settle_boundary(root,token)
    assert (root/boundary.BOUNDARY).read_bytes()==before
    assert path.read_bytes()==b'null\n'
