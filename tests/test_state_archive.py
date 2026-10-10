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
