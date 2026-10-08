"""Transport fixtures use a real approved repo row, never synthetic paper PASS."""
from datetime import date
import json
from pathlib import Path
import shutil
import pytest
from daily_agent.cloud_workflow import (init_profile, prepare_handoff, read_handoff,
    record_transition, bind_library, reconcile_publication, _config, _path)
from daily_agent.models import ApprovedItem, MaterialRecord
from daily_agent.workflow_state import StateCorrupt

DAY = date(2026, 10, 8)

@pytest.fixture
def report(tmp_path, monkeypatch):
    root = tmp_path / 'cloud'
    init_profile(Path(__file__).resolve().parents[1], root, 'codex')
    material = MaterialRecord(key='github:test/repo', source='github', item_type='repo',
                              title='Test repo', url='https://github.com/test/repo')
    row = ApprovedItem(key=material.key, item_type='repo', source='github', title=material.title,
                       url=material.url, final_fields={'what_it_is':'A test project'}, material=material).to_dict()
    monkeypatch.setattr('daily_agent.scheduling.validate_ready_report',
                        lambda *a: {'schema_version':3, 'approval':[row], 'sha256':'a'*64})
    return root


def prepared(root):
    return prepare_handoff(root, DAY, 'verified-conversation')


def bound(root, tmp_path):
    value = prepared(root)
    export = tmp_path / 'verified-export.html'
    shutil.copyfile(root / value['html_report'], export)
    return bind_library(root, DAY, file_id='library_fixture', version=1, verified_file=export)


def accepted(root, tmp_path):
    bound(root, tmp_path)
    sending = record_transition(root, DAY, 'begin')
    return record_transition(root, DAY, 'accepted', attempt_id=sending['attempt_id'], message_id='message_fixture')


def confirm(root, receipt, attachments):
    return record_transition(root, DAY, 'confirmed', attempt_id=receipt['attempt_id'],
       message_id=receipt['message_id'], conversation=receipt['conversation'], body=receipt['body'], attachments=attachments)


def test_default_html_seals_caption_and_attachment(report):
    value = prepared(report)
    assert value['schema_version'] == 2 and len(value['body']) < 400
    assert value['library_file_ids'] == [] and len(value['artifacts']) == 2
    assert 'A test project' not in value['body']
    assert 'A test project' in (report / value['html_report']).read_text()
    with pytest.raises(ValueError, match='Library'): record_transition(report, DAY, 'begin')
    assert read_handoff(report, DAY)['state'] == 'prepared'


def test_bind_begin_accept_retry_and_confirm(report, tmp_path):
    receipt = accepted(report, tmp_path)
    assert receipt['library_file_ids'] == ['library_fixture']
    retry = record_transition(report, DAY, 'accepted', attempt_id=receipt['attempt_id'], message_id=receipt['message_id'])
    assert retry == receipt
    with pytest.raises(ValueError): record_transition(report, DAY, 'begin')
    assert not receipt['publication_reconciled']
    with pytest.raises(ValueError): reconcile_publication(report, DAY)
    result = confirm(report, receipt, [{'file_id':'library_fixture', 'version':1}])
    assert result['state'] == 'confirmed' and result['publication_reconciled']


@pytest.mark.parametrize('attachments', [None, [], [{'file_id':'wrong','version':1}],
    [{'file_id':'library_fixture','version':2}], [{'file_id':'library_fixture','version':True}], [{'file_id':'library_fixture'}],
    [{'file_id':'library_fixture','version':1},{'file_id':'extra','version':1}]])
def test_missing_wrong_or_extra_attachment_never_confirms(report, tmp_path, attachments):
    receipt = accepted(report, tmp_path)
    with pytest.raises(ValueError, match='attachment'): confirm(report, receipt, attachments)
    result = read_handoff(report, DAY)
    assert result['state'] == 'accepted' and not result['publication_reconciled']
    assert not (_config(report).selected_dir / f'{DAY}.json').exists()


def test_wrong_caption_does_not_confirm(report, tmp_path):
    receipt = accepted(report, tmp_path); receipt['body'] += 'changed'
    with pytest.raises(ValueError, match='body'): confirm(report, receipt, [{'file_id':'library_fixture','version':1}])


def test_uploaded_bytes_must_match_sealed_html(report, tmp_path):
    prepared(report)
    export = tmp_path / 'bad.html'; export.write_text('wrong bytes')
    with pytest.raises(ValueError, match='bytes'): bind_library(report, DAY, file_id='library_fixture', version=1, verified_file=export)
    assert read_handoff(report, DAY)['library_binding'] is None


def test_binding_cannot_be_replaced(report, tmp_path):
    bound(report, tmp_path)
    with pytest.raises(ValueError, match='immutable'):
        bind_library(report, DAY, file_id='different', version=1, verified_file=tmp_path/'verified-export.html')


@pytest.mark.parametrize('stage', ['prepared', 'bound', 'accepted'])
def test_html_changed_after_seal_blocks_any_send_or_confirmation(report, tmp_path, stage):
    value = prepared(report) if stage == 'prepared' else bound(report, tmp_path) if stage == 'bound' else accepted(report, tmp_path)
    (report / value['html_report']).write_text('changed')
    with pytest.raises(StateCorrupt): read_handoff(report, DAY)
    with pytest.raises(StateCorrupt): record_transition(report, DAY, 'begin')


@pytest.mark.parametrize('field,value', [('kind','pilot'), ('source_sha256','b'*64),
    ('excluded',[{'key':'new'}]), ('html_report','data/other.html')])
def test_postseal_metadata_mutation_detected(report, field, value):
    prepared(report); path = _path(_config(report), DAY)
    manifest = json.loads(path.read_text()); manifest[field] = value; path.write_text(json.dumps(manifest))
    with pytest.raises((StateCorrupt, FileNotFoundError)): read_handoff(report, DAY)


def test_binding_version_tamper_detected(report, tmp_path):
    bound(report, tmp_path); path = _path(_config(report), DAY)
    manifest = json.loads(path.read_text()); manifest['library_binding']['version'] = 2; path.write_text(json.dumps(manifest))
    with pytest.raises(StateCorrupt): read_handoff(report, DAY)


def test_text_receipt_stays_legacy_and_prepared_is_never_upgraded(report):
    old = prepare_handoff(report, DAY, 'verified-conversation', delivery_format='text')
    path = _path(_config(report), DAY); before = path.read_bytes()
    assert old['schema_version'] == 1 and len(old['artifacts']) == 1
    assert prepared(report)['identity'] == old['identity'] and path.read_bytes() == before
    sending = record_transition(report, DAY, 'begin')
    receipt = record_transition(report, DAY, 'accepted', attempt_id=sending['attempt_id'], message_id='legacy')
    assert confirm(report, receipt, None)['state'] == 'confirmed'
    before = path.read_bytes()
    assert prepared(report)['state'] == 'confirmed' and path.read_bytes() == before


def test_uncertain_html_receipt_never_resends(report, tmp_path):
    bound(report, tmp_path); sending = record_transition(report, DAY, 'begin')
    record_transition(report, DAY, 'uncertain', attempt_id=sending['attempt_id'])
    with pytest.raises(ValueError): record_transition(report, DAY, 'begin')


def test_html_receipt_survives_root_relocation(report, tmp_path):
    value = bound(report, tmp_path)
    moved = tmp_path / 'moved'; shutil.copytree(report, moved)
    assert read_handoff(moved, DAY) == value


def test_cli_binding_and_begin_emit_exact_transport(report, tmp_path, capsys):
    from daily_agent.cloud_workflow import main
    value = prepared(report)
    export = tmp_path / 'export.html'; shutil.copyfile(report/value['html_report'], export)
    assert main(['bind-library','--root',str(report),'--date',str(DAY),
        '--library-file-id','library_cli','--library-version','3','--verified-library-file',str(export)]) == 0
    binding = json.loads(capsys.readouterr().out)
    assert binding['library_binding']['version'] == 3 and 'approval' not in binding
    assert main(['begin','--root',str(report),'--date',str(DAY)]) == 0
    sending = json.loads(capsys.readouterr().out)
    assert sending['body'] == value['body'] and sending['library_file_ids'] == ['library_cli']


def test_confirmed_html_cannot_lose_attachment_evidence(report, tmp_path):
    receipt = accepted(report, tmp_path)
    confirm(report, receipt, [{'file_id':'library_fixture','version':1}])
    path = _path(_config(report), DAY)
    manifest = json.loads(path.read_text()); manifest.pop('verified_attachments'); path.write_text(json.dumps(manifest))
    with pytest.raises(StateCorrupt, match='attachment'): read_handoff(report, DAY)


def test_confirming_does_not_mutate_sealed_approved_fields(report, tmp_path):
    receipt = accepted(report, tmp_path)
    before = json.dumps(receipt['approval'], sort_keys=True)
    confirmed = confirm(report, receipt, [{'file_id':'library_fixture','version':1}])
    assert json.dumps(confirmed['approval'], sort_keys=True) == before
    assert confirmed['approval_sha256'] == receipt['approval_sha256']
    assert confirmed['identity'] == receipt['identity']


def test_private_runtime_paths_redacted_without_breaking_source_urls():
    from daily_agent.cloud_workflow import _plain_text
    raw = 'Error /workspace/shared/private.pdf file:///home/agent/key.json C:\\Users\\secret\\file.txt; source https://example.org/workspace/paper'
    clean = _plain_text(raw)
    assert '/workspace/shared' not in clean and 'file://' not in clean and 'C:\\Users' not in clean
    assert 'https://example.org/workspace/paper' in clean


def test_initial_library_version_zero_roundtrip(report, tmp_path):
    value = prepared(report)
    export = tmp_path / 'library-version-zero.html'
    shutil.copyfile(report / value['html_report'], export)
    binding = bind_library(report, DAY, file_id='library_initial_zero', version=0, verified_file=export)
    assert binding['library_binding']['version'] == 0
    sending = record_transition(report, DAY, 'begin')
    receipt = record_transition(report, DAY, 'accepted', attempt_id=sending['attempt_id'], message_id='zero_version_message')
    result = confirm(report, receipt, [{'file_id':'library_initial_zero','version':0}])
    assert result['state'] == 'confirmed' and result['publication_reconciled']


@pytest.mark.parametrize('version', [-1, True, False, '0', 0.0, None])
def test_invalid_library_versions_rejected(report, tmp_path, version):
    value = prepared(report)
    export = tmp_path / 'export.html'; shutil.copyfile(report / value['html_report'], export)
    with pytest.raises(ValueError, match='integer version'):
        bind_library(report, DAY, file_id='library_invalid', version=version, verified_file=export)
    assert read_handoff(report, DAY)['library_binding'] is None
