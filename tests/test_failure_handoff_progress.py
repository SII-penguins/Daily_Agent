"""Failure status must distinguish unfinished delivery from zero qualified work."""
from copy import deepcopy
import json

import pytest

from daily_agent.cloud_workflow import _config, _failure_progress, prepare_handoff
from daily_agent.models import ApprovedItem, MaterialRecord
from daily_agent.scheduling import _ready_path
from daily_agent.workflow_state import atomic_json
from test_cloud_handoff import cloud, DAY
from test_native_claim_pixel_review import example
from test_native_scientific_pixels import native_science, as_item


def repository(key):
    material = MaterialRecord(key=key, source='github', item_type='repo', title=key,
                              url='https://github.com/test/' + key)
    return ApprovedItem(key, 'repo', key, 'github', material.url,
                        {'what_it_is': 'Offline verified repository fixture'}, material).to_dict()


def snapshot(config, rows):
    path = config.root / 'data/editorial' / str(DAY) / 'approval.json'
    atomic_json(path, rows)
    return path


@pytest.mark.parametrize('failure', ['独立审核仍有等待项', ''])
def test_unsealed_qualified_work_is_counted_without_promoting_or_sealing(cloud, failure):
    config = _config(cloud)
    path = snapshot(config, [repository('one'), repository('two')])
    before = path.read_bytes()
    result = prepare_handoff(cloud, DAY, 'verified-conversation', failure=failure)
    assert '本期未完成可交付日报' in result['body']
    assert '已核验完成 2 条' in result['body'] and '尚未封版 2 条' in result['body']
    assert ('阻塞：' + (failure or '运行未完成，具体阻塞未提供')) in result['body']
    assert '没有满足全文核验' not in result['body']
    assert result['kind'] == 'status' and result['approval'] == []
    assert path.read_bytes() == before
    assert not _ready_path(config, DAY).exists()
    assert not (config.state_dir / 'published_index.json').exists()


def test_missing_progress_is_unknown_not_zero_qualified(cloud):
    result = prepare_handoff(cloud, DAY, 'verified-conversation', failure='执行时间到期')
    assert '数量尚未核实' in result['body']
    assert '已核验完成 0 条' not in result['body']
    assert '没有满足全文核验' not in result['body']


@pytest.mark.parametrize('mode', ['malformed_json', 'wrong_shape', 'duplicate'])
def test_invalid_progress_stays_unknown_and_status_only(cloud, mode):
    config = _config(cloud)
    path = snapshot(config, [repository('one')])
    if mode == 'malformed_json': path.write_text('{')
    elif mode == 'wrong_shape': atomic_json(path, {'wrong': []})
    else: atomic_json(path, [repository('one'), repository('one')])
    result = prepare_handoff(cloud, DAY, 'verified-conversation', failure='审批快照写入未完成')
    assert '数量尚未核实' in result['body'] and result['approval'] == []
    assert not _ready_path(config, DAY).exists()


def test_invalid_ready_marker_is_not_counted_as_a_seal(cloud):
    config = _config(cloud)
    snapshot(config, [repository('one')])
    atomic_json(_ready_path(config, DAY), {'malformed': True})
    result = prepare_handoff(cloud, DAY, 'verified-conversation', failure='封版校验失败')
    assert '已核验完成 1 条' in result['body']
    assert '封版数量尚未核实' in result['body']
    assert '已核验封版 1 条' not in result['body']


def test_existing_verified_seal_and_unsealed_snapshot_are_distinguished(cloud, monkeypatch):
    config = _config(cloud)
    one, two = repository('one'), repository('two')
    snapshot(config, [one, two])
    atomic_json(_ready_path(config, DAY), {'fixture_exists': True})
    monkeypatch.setattr('daily_agent.scheduling.validate_ready_report',
                        lambda *a: {'schema_version': 3, 'approval': [one]})
    progress = _failure_progress(config, DAY)
    assert '已核验完成 2 条' in progress
    assert '已核验封版 1 条' in progress and '尚未封版 1 条' in progress


def test_native_core_only_does_not_inflate_completed_progress(native_science):
    record, draft, config, _ = native_science
    config.state_dir = config.root / 'data/state'
    record.detail = deepcopy(draft.draft_fields)
    snapshot(config, [as_item(record, draft).to_dict()])
    progress = _failure_progress(config, DAY)
    assert '已核验完成 0 条' in progress
    assert '1 条审批候选未通过当前交付门槛' in progress
