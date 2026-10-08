"""No ambiguous transport result is silently published or sent again."""
from datetime import timedelta
import copy
import json
import pytest
import yaml
from test_cloud_html_handoff import report, DAY, prepared, accepted, bound, confirm
from daily_agent.cloud_workflow import (record_transition, read_handoff, prepare_handoff,
    bind_library, reserved_delivery_identities, _config, _path)
from daily_agent.models import MaterialRecord
from daily_agent.storage import select_library_candidates
from daily_agent.workflow_state import StateCorrupt, WorkflowBusy, exclusive_lock

OBS = {'partial':True, 'attachments':[{'attachment_id':'opaque-attachment-1',
        'target':'CalpicoFile_fixture', 'type':'file'}]}


def observe(root, receipt, **changes):
    values = dict(attempt_id=receipt['attempt_id'], message_id=receipt['message_id'],
                  conversation=receipt['conversation'], body=receipt['body'], observation=copy.deepcopy(OBS))
    values.update(changes)
    return record_transition(root, DAY, 'observe-readback', **values)


def test_partial_readback_is_truthful_and_does_not_publish(report, tmp_path):
    receipt = accepted(report, tmp_path)
    observed = observe(report, receipt)
    assert observed['state'] == 'accepted' and not observed['publication_reconciled']
    assert observed['readback_observation']['partial'] is True
    assert observed['readback_observation']['attachment_identity_verified'] is False
    assert observed['readback_observation']['attachments'] == OBS['attachments']
    assert 'file_id' not in observed['readback_observation']['attachments'][0]
    assert observe(report, receipt) == observed
    with pytest.raises(ValueError): record_transition(report, DAY, 'begin')
    with pytest.raises(ValueError): confirm(report, observed, None)


@pytest.mark.parametrize('change', [{'body':'wrong'}, {'conversation':'wrong'}, {'message_id':'wrong'},
    {'attempt_id':'wrong'}, {'observation':{'partial':False,'attachments':OBS['attachments']}},
    {'observation':{'partial':True,'attachments':[]}},
    {'observation':{'partial':True,'attachments':[{'file_id':'invented','version':0}]}},
    {'observation':{'partial':True,'attachments':[{'attachment_id':'x','target':'','type':'file'}]}}])
def test_wrong_caption_destination_message_or_references_rejected(report, tmp_path, change):
    receipt = accepted(report, tmp_path)
    with pytest.raises(ValueError): observe(report, receipt, **change)
    assert 'readback_observation' not in read_handoff(report, DAY)


def test_reference_replacement_and_stored_tampering_rejected(report, tmp_path):
    receipt = accepted(report, tmp_path); observe(report, receipt)
    changed = copy.deepcopy(OBS); changed['attachments'][0]['target'] = 'different'
    with pytest.raises(ValueError, match='immutable'): observe(report, receipt, observation=changed)
    path = _path(_config(report), DAY); value = json.loads(path.read_text())
    value['readback_observation']['attachments'][0]['target'] = 'different'
    path.write_text(json.dumps(value))
    with pytest.raises(StateCorrupt): read_handoff(report, DAY)


@pytest.mark.parametrize('state', ['sending','uncertain','accepted'])
def test_unresolved_send_reserves_next_issue_before_reading(report, tmp_path, state):
    bound(report, tmp_path); receipt = record_transition(report, DAY, 'begin')
    if state == 'uncertain': record_transition(report, DAY, state, attempt_id=receipt['attempt_id'])
    if state == 'accepted': record_transition(report, DAY, state, attempt_id=receipt['attempt_id'], message_id='msg')
    cfg = _config(report); next_day = DAY + timedelta(days=1)
    assert 'github:test/repo' in reserved_delivery_identities(cfg, next_day)
    assert reserved_delivery_identities(cfg, DAY) == set()
    material = MaterialRecord.from_dict(receipt['approval'][0]['material'])
    assert select_library_candidates(cfg, {material.key:material}, next_day) == []
    with pytest.raises(ValueError, match='reserved'):
        prepare_handoff(report, next_day, receipt['conversation'])
    assert not _path(cfg, next_day).exists()


def test_two_prepared_issues_cannot_send_same_items(report, tmp_path):
    first = bound(report, tmp_path)
    next_day = DAY + timedelta(days=1)
    other = prepare_handoff(report, next_day, first['conversation'])
    exported = tmp_path/'second-export.html'; exported.write_bytes((report/other['html_report']).read_bytes())
    bind_library(report, next_day, file_id='second_library', version=0, verified_file=exported)
    record_transition(report, DAY, 'begin')
    with pytest.raises(ValueError, match='reserved'): record_transition(report, next_day, 'begin')
    assert read_handoff(report, next_day)['state'] == 'prepared'


def test_global_dispatch_lock_blocks_another_issue(report, tmp_path):
    bound(report, tmp_path)
    with exclusive_lock(_config(report).state_dir/'cloud-dispatch.lock'):
        with pytest.raises(WorkflowBusy): record_transition(report, DAY, 'begin')
    assert read_handoff(report, DAY)['state'] == 'prepared'


def test_corrupt_other_issue_blocks_reservation_scan(report, tmp_path):
    bound(report, tmp_path)
    other = _path(_config(report), DAY+timedelta(days=1)); other.write_text('{broken')
    with pytest.raises(StateCorrupt): record_transition(report, DAY, 'begin')
    with pytest.raises(StateCorrupt): reserved_delivery_identities(_config(report), DAY)
    assert read_handoff(report, DAY)['state'] == 'prepared'


def test_pilot_reservations_do_not_leak_to_production(report, tmp_path):
    path = report/'config/delivery.yaml'; config=yaml.safe_load(path.read_text())
    config['cloud']['pilot']=True; path.write_text(yaml.safe_dump(config))
    accepted(report, tmp_path)
    # Even if that valid pilot ledger is seen after a profile configuration change,
    # its sealed kind remains pilot and must not create production reservations.
    config['cloud']['pilot']=False; path.write_text(yaml.safe_dump(config))
    assert reserved_delivery_identities(_config(report), DAY+timedelta(days=1)) == set()


def test_confirmed_reconciled_items_use_existing_publication_history(report, tmp_path):
    receipt=accepted(report,tmp_path)
    confirm(report,receipt,[{'file_id':'library_fixture','version':1}])
    assert reserved_delivery_identities(_config(report),DAY+timedelta(days=1)) == set()
    from daily_agent.storage import load_material_library
    library=load_material_library(_config(report))
    assert select_library_candidates(_config(report),library,DAY+timedelta(days=1)) == []


def test_unreconciled_confirmation_remains_reserved(report, tmp_path, monkeypatch):
    receipt=accepted(report,tmp_path)
    monkeypatch.setattr('daily_agent.cloud_workflow.reconcile_publication',lambda *a:None)
    confirm(report,receipt,[{'file_id':'library_fixture','version':1}])
    assert 'github:test/repo' in reserved_delivery_identities(_config(report),DAY+timedelta(days=1))


def test_stale_prepared_issue_blocked_after_other_issue_confirms(report, tmp_path):
    bound(report,tmp_path)
    next_day=DAY+timedelta(days=1)
    other=prepare_handoff(report,next_day,'verified-conversation')
    export=tmp_path/'next-export.html';export.write_bytes((report/other['html_report']).read_bytes())
    bind_library(report,next_day,file_id='next_library',version=0,verified_file=export)
    sending=record_transition(report,DAY,'begin')
    receipt=record_transition(report,DAY,'accepted',attempt_id=sending['attempt_id'],message_id='prior_message')
    confirm(report,receipt,[{'file_id':'library_fixture','version':1}])
    with pytest.raises(ValueError,match='stale'):record_transition(report,next_day,'begin')
    assert read_handoff(report,next_day)['state']=='prepared'


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None])
def test_malformed_reconciliation_flag_cannot_release_reservation(report, tmp_path, monkeypatch, value):
    receipt=accepted(report,tmp_path)
    monkeypatch.setattr('daily_agent.cloud_workflow.reconcile_publication',lambda *a:None)
    confirm(report,receipt,[{'file_id':'library_fixture','version':1}])
    path=_path(_config(report),DAY);manifest=json.loads(path.read_text())
    manifest['publication_reconciled']=value;path.write_text(json.dumps(manifest))
    with pytest.raises(StateCorrupt):reserved_delivery_identities(_config(report),DAY+timedelta(days=1))
