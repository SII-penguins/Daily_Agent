from datetime import date
from pathlib import Path
import json
import pytest
from daily_agent.cloud_workflow import (init_profile, prepare_handoff, read_handoff, record_transition,
                                        reconcile_publication, _config, _path, _plain_text)
from daily_agent.workflow_state import StateCorrupt

DAY = date(2026,10,8)
SOURCE = Path(__file__).resolve().parents[1]

@pytest.fixture
def cloud(tmp_path):
    root = tmp_path / 'cloud'
    init_profile(SOURCE, root, '/opt/codex/bin/codex')
    return root

def ready(root):
    return prepare_handoff(root, DAY, 'verified-conversation', failure='测试运行超时')

def test_profile_is_separate_and_does_not_lower_evidence_gates(cloud):
    cfg = _config(cloud)
    base = __import__('daily_agent.config',fromlist=['load_config']).load_config(SOURCE)
    assert cfg.sources['reading'] == base.sources['reading']
    assert cfg.quota == base.quota
    assert not cfg.sources['google_scholar']['enabled']
    assert not cfg.delivery['delivery']['feishu']['enabled']
    assert cfg.sources['llm_writer']['command'] == '/opt/codex/bin/codex'
    assert cfg.delivery['cloud']['source_profile_is_full'] is False
    with pytest.raises(ValueError): init_profile(SOURCE, cloud, 'codex')

def test_accepted_is_not_confirmed_and_duplicate_send_blocked(cloud):
    prepared = ready(cloud)
    sending = record_transition(cloud,DAY,'begin')
    attempt = sending['attempt_id']
    accepted = record_transition(cloud,DAY,'accepted',attempt_id=attempt,message_id='Sentinel_fixture')
    assert accepted['state'] == 'accepted'
    assert not accepted['publication_reconciled']
    with pytest.raises(ValueError): record_transition(cloud,DAY,'begin')
    with pytest.raises(ValueError): reconcile_publication(cloud,DAY)
    with pytest.raises(ValueError):
        record_transition(cloud,DAY,'confirmed',attempt_id=attempt,message_id='Sentinel_fixture',conversation='other',body=prepared['body'])
    confirmed=record_transition(cloud,DAY,'confirmed',attempt_id=attempt,message_id='Sentinel_fixture',conversation='verified-conversation',body=prepared['body'])
    assert confirmed['state']=='confirmed' and confirmed['publication_reconciled']
    assert not (_config(cloud).state_dir/'published_index.json').exists()

def test_ambiguous_delivery_stays_blocked_and_preserves_snapshot(cloud):
    original=ready(cloud)
    sending=record_transition(cloud,DAY,'begin')
    record_transition(cloud,DAY,'uncertain',attempt_id=sending['attempt_id'])
    assert prepare_handoff(cloud,DAY,'verified-conversation',failure='new failure')['identity']==original['identity']
    with pytest.raises(ValueError): record_transition(cloud,DAY,'begin')

def test_body_tamper_rejected(cloud):
    p=ready(cloud)
    (cloud/p['report']).write_text('changed')
    with pytest.raises(StateCorrupt): read_handoff(cloud,DAY)

def test_approval_tamper_rejected(cloud):
    ready(cloud)
    path=_path(_config(cloud),DAY)
    p=json.loads(path.read_text()); p['approval']=[{'key':'fake'}]
    path.write_text(json.dumps(p))
    with pytest.raises(StateCorrupt): read_handoff(cloud,DAY)

def test_root_relative_artifact_survives_relocation(cloud,tmp_path):
    p=ready(cloud)
    import shutil
    moved=tmp_path/'moved'; shutil.copytree(cloud,moved)
    assert read_handoff(moved,DAY)['body']==p['body']

def test_plain_text_never_exposes_local_file_links():
    assert _plain_text('# Report\n[PDF](/private/file.pdf) [Paper](https://example.org/paper)') == 'Report\nPDF Paper (https://example.org/paper)\n'

def test_writer_never_disables_rules():
    from daily_agent.editorial import _llm_writer_command
    assert '--ignore-rules' not in _llm_writer_command({'command':'codex'},'test')

@pytest.mark.parametrize('change',[{'path':'../../escape'},{'sha256':'bad'},{'size':-1},{'media_type':'unknown'}])
def test_artifact_manifest_tamper_rejected(cloud,change):
    ready(cloud); path=_path(_config(cloud),DAY)
    p=json.loads(path.read_text()); p['artifacts'][0].update(change); path.write_text(json.dumps(p))
    with pytest.raises(StateCorrupt): read_handoff(cloud,DAY)

def test_generation_journals_launch_and_cleans_previous_day(cloud,monkeypatch):
    from daily_agent.cloud_workflow import run_generation
    from daily_agent.workflow_state import atomic_json
    import daily_agent.workflow_runtime as runtime
    cfg=_config(cloud);path=cfg.state_dir/'cloud-generation.json'
    identity={'pid':98765,'pgid':98765,'description':'owned process'}
    import daily_agent.cloud_workflow as workflow
    from datetime import datetime, timezone
    monkeypatch.setattr(workflow,'_clock',lambda:datetime(2026,10,7,17,tzinfo=timezone.utc))
    atomic_json(path,{'date':'2026-10-07','running':True,'child_identity':identity,'attempt_id':'previous'})
    atomic_json(cfg.state_dir/'cloud-generation-budgets/2026-10-07.json',{'schema_version':1,'date':'2026-10-07','issue_started_at':'2026-10-07T16:59:00+00:00','deadline':'2026-10-07T23:48:00+00:00','active_started_at':'2026-10-07T16:59:00+00:00','active_attempt_id':'previous','runtime_seconds':0,'failures':0,'resumes':1,'max_runtime_seconds':10000,'max_failures':3,'max_resumes':200})
    seen=[]
    monkeypatch.setattr(runtime,'stop_verified_orphan',lambda value,expected_root:seen.append(value))
    def run(*args,**kwargs):
        kwargs['on_start'](identity);kwargs['on_tick']();return 75
    monkeypatch.setattr(runtime,'run_process',run)
    assert run_generation(cloud,DAY,30)==75
    assert seen==[identity]
    assert json.loads(path.read_text())['running'] is False

def test_legacy_commands_cannot_overwrite_cloud_profile(cloud):
    from daily_agent.cli import main
    from daily_agent.full_profile import enforce_full_profile
    before=(cloud/'config/sources.yaml').read_bytes()
    assert main(['run','--root',str(cloud),'--dry-run'])==2
    assert main(['deliver-ready','--root',str(cloud)])==2
    with pytest.raises(ValueError): enforce_full_profile(cloud,write=True)
    assert (cloud/'config/sources.yaml').read_bytes()==before

def test_parent_writer_readiness_is_explicitly_nonfull(cloud):
    from daily_agent.quality import run_quality_check
    profile=run_quality_check(_config(cloud))
    writer=next(value for value in profile.checks if value.key=='llm_writer')
    assert writer.status=='fallback' and writer.name=='Parent-assisted writer'

def test_generation_failure_budget_does_not_reset(cloud,monkeypatch):
    import daily_agent.cloud_workflow as workflow
    import daily_agent.workflow_runtime as runtime
    from datetime import datetime,timezone,timedelta
    clock=[datetime(2026,10,7,17,tzinfo=timezone.utc)]
    monkeypatch.setattr(workflow,'_clock',lambda:clock[0])
    calls=[]
    def run(*args,**kwargs):
        calls.append(args[2]);clock[0]+=timedelta(seconds=2);return 124
    monkeypatch.setattr(runtime,'run_process',run)
    assert [workflow.run_generation(cloud,DAY,900) for _ in range(4)]==[124]*4
    assert len(calls)==3
    budget=json.loads((_config(cloud).state_dir/f'cloud-generation-budgets/{DAY}.json').read_text())
    assert budget['failures']==3 and budget['runtime_seconds']>=6

def test_pending_charged_runtime_but_not_failure_and_deadline_is_fixed(cloud,monkeypatch):
    import daily_agent.cloud_workflow as workflow
    import daily_agent.workflow_runtime as runtime
    from datetime import datetime,timezone,timedelta
    clock=[datetime(2026,10,7,23,47,59,tzinfo=timezone.utc)]
    monkeypatch.setattr(workflow,'_clock',lambda:clock[0])
    calls=[]
    def run(*args,**kwargs):
        calls.append(args[2]);clock[0]+=timedelta(seconds=2);return 75
    monkeypatch.setattr(runtime,'run_process',run)
    assert workflow.run_generation(cloud,DAY,900)==75
    assert workflow.run_generation(cloud,DAY,900)==124
    budget=json.loads((_config(cloud).state_dir/f'cloud-generation-budgets/{DAY}.json').read_text())
    assert calls==[1] and budget['failures']==0 and budget['runtime_seconds']>=2
    assert budget['deadline']=='2026-10-07T23:48:00+00:00'

def test_historical_nonpilot_never_launches(cloud,monkeypatch):
    import daily_agent.cloud_workflow as workflow
    import daily_agent.workflow_runtime as runtime
    monkeypatch.setattr(runtime,'run_process',lambda *a,**k:pytest.fail('expired issue launched'))
    assert workflow.run_generation(cloud,date(2020,1,1),900)==124

def test_recipe_section_is_method_evidence_not_automatic_acceptance():
    from daily_agent.paper_document import section_kind
    assert section_kind('2. Recipes for Building Strong VLA Models')=='method'

def test_issue_date_defaults_to_configured_timezone(cloud,monkeypatch):
    import daily_agent.cloud_workflow as workflow
    from datetime import datetime,timezone
    monkeypatch.setattr(workflow,'_clock',lambda:datetime(2026,10,7,16,10,tzinfo=timezone.utc))
    assert workflow._local_day(cloud)==date(2026,10,8)

@pytest.mark.parametrize('field,value',[('failures',-50),('resumes',False),('max_resumes',0),('max_runtime_seconds',float('inf')),('deadline','2026-10-08T07:48:00')])
def test_malformed_issue_budget_blocks_before_launch(cloud,monkeypatch,field,value):
    import daily_agent.cloud_workflow as workflow
    import daily_agent.workflow_runtime as runtime
    from datetime import datetime,timezone
    monkeypatch.setattr(workflow,'_clock',lambda:datetime(2026,10,7,17,tzinfo=timezone.utc))
    monkeypatch.setattr(runtime,'run_process',lambda *a,**k:0)
    workflow.run_generation(cloud,DAY,900)
    path=_config(cloud).state_dir/f'cloud-generation-budgets/{DAY}.json'
    budget=json.loads(path.read_text());budget[field]=value;path.write_text(json.dumps(budget))
    monkeypatch.setattr(runtime,'run_process',lambda *a,**k:pytest.fail('corrupt budget launched'))
    with pytest.raises(StateCorrupt):workflow.run_generation(cloud,DAY,900)

def test_launch_failure_consumes_failure_and_remains_recoverable(cloud,monkeypatch):
    import daily_agent.cloud_workflow as workflow
    import daily_agent.workflow_runtime as runtime
    from datetime import datetime,timezone
    monkeypatch.setattr(workflow,'_clock',lambda:datetime(2026,10,7,17,tzinfo=timezone.utc))
    def fail(*a,**k):raise OSError('Popen unavailable')
    monkeypatch.setattr(runtime,'run_process',fail)
    assert workflow.run_generation(cloud,DAY,900)==1
    monkeypatch.setattr(runtime,'run_process',lambda *a,**k:75)
    assert workflow.run_generation(cloud,DAY,900)==75

def test_resume_capacity_covers_eight_paper_evidence_workload(cloud):
    from daily_agent.cloud_workflow import _resume_capacity
    assert _resume_capacity(_config(cloud))==4584

def test_prepared_issue_is_immutable_before_send(cloud):
    original=ready(cloud)
    updated=prepare_handoff(cloud,DAY,'verified-conversation',failure='a new status')
    assert updated['identity']==original['identity'] and updated['body']==original['body']

def test_profile_rejects_storage_path_escape(cloud,tmp_path):
    import yaml
    path=cloud/'config/delivery.yaml';value=yaml.safe_load(path.read_text())
    value['report']['state_dir']=str(tmp_path/'outside')
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):_config(cloud)

def test_cli_compact_output_preserves_body_and_identity_without_large_approval(cloud,monkeypatch,capsys):
    import daily_agent.cloud_workflow as workflow
    from daily_agent.models import ApprovedItem,MaterialRecord
    payload=ready(cloud)
    material=MaterialRecord(key='github:a/large',source='github',item_type='repo',title='a/large',url='https://github.com/a/large',readme_excerpt='large source evidence '*100000)
    payload['approval']=[ApprovedItem(key=material.key,item_type='repo',title=material.title,source='github',url=material.url,final_fields={'what_it_is':'fixture'},material=material).to_dict()]
    calls=[]
    def validated_read(root,day):
        calls.append((root,day));return payload
    monkeypatch.setattr(workflow,'read_handoff',validated_read)
    assert workflow.main(['read','--root',str(cloud),'--date',str(DAY)])==0
    rendered=capsys.readouterr().out;value=json.loads(rendered)
    assert calls==[(str(cloud),DAY)] and len(rendered)<10000
    assert 'approval' not in value and value['approval_count']==1
    for field in ('body','state','identity','body_sha256','approval_sha256'):
        assert value[field]==payload[field]
    assert len(payload['approval'][0]['material']['readme_excerpt'])>1000000
    assert workflow.main(['read','--root',str(cloud),'--date',str(DAY),'--include-evidence'])==0
    expanded=json.loads(capsys.readouterr().out)
    assert expanded['approval']==payload['approval']

def test_cli_begin_and_existing_prepare_use_compact_output_without_second_attempt(cloud,capsys):
    import daily_agent.cloud_workflow as workflow
    prepared=ready(cloud)
    assert workflow.main(['begin','--root',str(cloud),'--date',str(DAY)])==0
    begun=json.loads(capsys.readouterr().out)
    assert begun['state']=='sending' and 'approval' not in begun
    assert begun['approval_count']==0 and begun['body']==prepared['body']
    assert workflow.main(['prepare','--root',str(cloud),'--date',str(DAY),'--conversation','verified-conversation'])==0
    recovered=json.loads(capsys.readouterr().out)
    assert recovered['attempt_id']==begun['attempt_id'] and 'approval' not in recovered

def test_compact_cli_still_rejects_tampered_stored_body(cloud,capsys):
    import daily_agent.cloud_workflow as workflow
    prepared=ready(cloud);(cloud/prepared['report']).write_text('tampered')
    with pytest.raises(StateCorrupt):workflow.main(['read','--root',str(cloud),'--date',str(DAY)])
    assert capsys.readouterr().out==''

def test_prepare_exposes_expired_writer_job_from_current_attempt(cloud,monkeypatch,capsys):
    import daily_agent.cloud_workflow as workflow
    from daily_agent.workflow_state import atomic_json
    job='a'*64
    def generation(root,day,timeout):
        atomic_json(_config(root).state_dir/'cloud-generation.json',{'date':str(day),'checkpoint':{'state':'expired_parent_writer','job_id':job,'issue_date':str(day)}})
        return 75
    monkeypatch.setattr(workflow,'run_generation',generation)
    assert workflow.main(['prepare','--root',str(cloud),'--date',str(DAY),'--conversation','verified'])==75
    payload=json.loads(capsys.readouterr().out)
    assert payload['state']=='expired_parent_writer' and payload['job_id']==job and payload['issue_date']==str(DAY)
