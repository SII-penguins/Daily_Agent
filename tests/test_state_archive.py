from datetime import date
from pathlib import Path
import pytest
from daily_agent.cloud_workflow import init_profile,prepare_handoff,record_transition,read_handoff
from daily_agent.state_archive import snapshot,restore,verify
from daily_agent.workflow_state import StateCorrupt


def test_state_roundtrip_preserves_accepted_receipt_and_never_overwrites(tmp_path):
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    day=date(2026,10,8);prepare_handoff(root,day,'verified',failure='fixture')
    sending=record_transition(root,day,'begin')
    record_transition(root,day,'accepted',attempt_id=sending['attempt_id'],message_id='message-fixture')
    saved=snapshot(root,tmp_path/'state.zip');verify(saved['archive'])
    target=tmp_path/'restored';restore(saved['archive'],target,saved['sha256'])
    assert read_handoff(target,day)['state']=='accepted'
    with pytest.raises(StateCorrupt, match='fenced'):record_transition(target,day,'begin')
    with pytest.raises(FileExistsError):restore(saved['archive'],target,saved['sha256'])
    with pytest.raises(StateCorrupt):restore(saved['archive'],tmp_path/'wrong','bad')


def test_snapshot_refuses_credential_files_without_reading_them(tmp_path):
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    (root/'auth.json').write_text('fixture only')
    with pytest.raises(ValueError):snapshot(root,tmp_path/'state.zip')

@pytest.mark.parametrize('names',[['data/ledger.json','data/./ledger.json'],['data','data/ledger.json']])
def test_archive_rejects_normalized_path_and_prefix_collisions(tmp_path,names):
    import zipfile,json,hashlib
    archive=tmp_path/'bad.zip';content=b'{}'
    manifest={'schema_version':1,'profile':'cloud-public-chatgpt-v1','files':{name:{'size':2,'sha256':hashlib.sha256(content).hexdigest()} for name in names}}
    with zipfile.ZipFile(archive,'w') as output:
        for name in names:output.writestr(name,content)
        output.writestr('STATE-MANIFEST.json',json.dumps(manifest))
    with pytest.raises(StateCorrupt):verify(archive)


def test_scientific_null_repair_receipt_roundtrip_is_byte_exact(tmp_path):
    import hashlib, zipfile
    from daily_agent.durable_recovery import inspect_recovery
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    relative='data/scientific-analysis/'+'a'*64+'/writer-repair.json'
    path=root/relative;path.parent.mkdir(parents=True);content=b'  null\n'
    path.write_bytes(content)
    saved=snapshot(root,tmp_path/'state.zip')
    manifest=verify(saved['archive'])
    assert manifest['files'][relative]=={'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    with zipfile.ZipFile(saved['archive']) as archive:assert archive.read(relative)==content
    target=tmp_path/'restored';restore(saved['archive'],target,saved['sha256'])
    assert (target/relative).read_bytes()==path.read_bytes()==content
    assert not inspect_recovery(target)['active_evidence']


@pytest.mark.parametrize('relative',[
    'data/state/cloud-generation.json',
    'data/state/cloud-generation-budgets/2026-10-10.json',
    'data/state/cloud-delivery/2026-10-10.json',
    'data/state/production-revisions/2026-10-10/revision/generation.json',
    'data/state/schedule-state.json',
    'data/ready-reports/2026-10-10.outbox.json',
    'data/state/writer-repair.json',
    'data/scientific-analysis/not-a-valid-identity/writer-repair.json',
    'data/scientific-analysis/'+'a'*64+'/nested/writer-repair.json',
])
def test_required_or_wrong_path_null_records_fail_closed(tmp_path,relative):
    import json
    from daily_agent.durable_recovery import FENCE,inspect_recovery
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'null\n')
    with pytest.raises(StateCorrupt,match='Null workflow record'):snapshot(root,tmp_path/'state.zip')
    (root/FENCE).write_text(json.dumps({'original_state_root':str(root)}))
    assert {'path':relative,'reason':'unreadable_json'} in inspect_recovery(root)['active_evidence']
    assert path.read_bytes()==b'null\n' and not (tmp_path/'state.zip').exists()


@pytest.mark.parametrize('content',[b'{',b'NaN',b'{"repair":null,"repair":null}'])
def test_optional_repair_path_does_not_exempt_invalid_json(tmp_path,content):
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    path=root/'data/scientific-analysis'/('a'*64)/'writer-repair.json'
    path.parent.mkdir(parents=True);path.write_bytes(content)
    with pytest.raises(StateCorrupt):snapshot(root,tmp_path/'state.zip')
    assert path.read_bytes()==content
