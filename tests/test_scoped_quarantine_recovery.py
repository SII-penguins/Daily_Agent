from copy import deepcopy
from datetime import datetime,timedelta,timezone,date
import hashlib,json,os
from pathlib import Path

import pytest

from daily_agent.cloud_workflow import init_profile
from daily_agent import claim_quarantine as quarantine,durable_recovery as recovery
from daily_agent.parent_writer import digest
from daily_agent.workflow_state import StateCorrupt,atomic_json,atomic_bytes,assert_issue_allowed


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2))


@pytest.fixture
def scoped(tmp_path):
    root=tmp_path/'state';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    now=datetime.now(timezone.utc);newday=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date()
    oldday=newday-timedelta(days=3)
    job={'schema_version':1,'transport':'parent_assisted','prompt':'exact expired historical draft','images':[],'stage':'draft'}
    job_id=digest(job);created=now-timedelta(days=3)
    job.update(job_id=job_id,input_sha256=job_id,created_at=created.isoformat(),expires_at=(created+timedelta(hours=6)).isoformat(),call_timeout_seconds=120)
    claim={'job_id':job_id,'worker_id':'unknown-old-worker','generation':7,'token':'fixture-token','expires_at':(created+timedelta(minutes=15)).isoformat()}
    q=root/'data/writer-queue';write(q/f'{job_id}.job.json',job);write(q/f'{job_id}.claim.json',claim)
    write(root/f'data/state/cloud-budget/{oldday}.json',{'date':str(oldday),'remaining':0,'failures':7,'deadline':(created+timedelta(hours=6)).isoformat()})
    receipts={kind:{'library_file_id':f'libfile_fixture_{kind}','file_id':f'fixture_{kind}','version':0,'sha256':'a'*64,'size':10} for kind in ('source','state')}
    fence={'schema_version':1,'status':'blocked','reason':'workspace_loss_restore','receipts':receipts,
           'original_state_root':str(root),'restored_at':now.isoformat(),
           'requires':['verify_previous_executor_stopped','reconcile_claim_owners','inspect_unresolved_delivery','verify_site_archive_owner','revalidate_absolute_paths'],
           'budgets_attempts_claims_answers_history_preserved':True}
    write(root/recovery.FENCE,fence)
    scope={'issue_date':str(newday),'deadline':(now+timedelta(hours=2)).isoformat(),'current_owner':'/root/current',
           'current_executor_ref':'verified-new-executor:fixture','authorization_ref':'user:authorized-distinct-issue','old_owner_status':'unknown'}
    hashes={f'expected_{kind}_sha256':hashlib.sha256((q/f'{job_id}.{kind}.json').read_bytes()).hexdigest() for kind in ('job','claim')}
    quarantine.create_quarantine(root,job_id,scope,**hashes)
    outcomes={'verify_previous_executor_stopped':'current_executor_verified_old_owner_unknown_quarantined','reconcile_claim_owners':'resolved',
              'inspect_unresolved_delivery':'verified_no_uncertain_sends','verify_site_archive_owner':'diagnostic_no_site','revalidate_absolute_paths':'original_root_verified'}
    review={'receipts':receipts,'reviewer':'verified-current-controller','new_issue_scope':scope,
            'evidence':{k:{'outcome':v,'reference':'actual isolated fixture evidence'} for k,v in outcomes.items()},
            'claims':[{'job_id':job_id,'worker_id':claim['worker_id'],'generation':7,'outcome':'quarantined_expired_unknown','reference':'verified immutable quarantine'}]}
    return root,job_id,scope,review,oldday,job


def release(scoped):
    root,job_id,scope,review,oldday,job=scoped
    recovery.release_recovery(root,review)
    return root,job_id,scope,oldday,job


def fake_executing(root):
    # Simulates the verified boundary in gate-unit tests; remote evidence has its
    # separate full contract suite. This is not a remote provenance assertion.
    write(root/'.daily-agent-boundary.json',{'state_root':str(root),'phase':'executing'})


def test_scoped_release_keeps_unknown_status_and_requires_intent(scoped):
    root,job_id,scope,oldday,_=release(scoped)
    assert not(root/recovery.FENCE).exists()
    stored=json.loads((root/'.daily-agent-recovery-scope.json').read_text())
    assert stored['old_owner_status']=='unknown' and not stored['process_death_claimed']
    assert not stored['historical_issues_released']
    with pytest.raises(StateCorrupt,match='durable batch intent'):atomic_json(root/'new.json',{})
    assert quarantine.validate_quarantine(root,job_id,scope)['old_owner_status']=='unknown'
    assert_issue_allowed(root,date.fromisoformat(scope['issue_date']))
    with pytest.raises(StateCorrupt,match='old issues'):assert_issue_allowed(root,oldday)


def test_same_prompt_and_old_claim_import_retry_never_reopen(scoped):
    from daily_agent.parent_writer import claim,import_response,retry_expired,request
    root,jid,scope,oldday,job=release(scoped);fake_executing(root)
    original=(root/f'data/writer-queue/{jid}.claim.json').read_bytes()
    actions=[lambda:claim(root,jid,'new-worker'),lambda:import_response(root,jid,{},'unknown-old-worker'),
             lambda:retry_expired(root,jid,date.fromisoformat(scope['issue_date']),'must not retry'),
             lambda:request(root,job['prompt'],120,stage='draft')]
    for action in actions:
        with pytest.raises(StateCorrupt,match='Quarantined'):action()
    assert (root/f'data/writer-queue/{jid}.claim.json').read_bytes()==original
    assert not(root/f'data/writer-queue/{jid}.answer.json').exists()


def test_old_date_entrypoints_block_before_launch_publish_or_budget_reset(scoped):
    from daily_agent.cloud_workflow import run_generation,generate,prepare_handoff,record_transition,reconcile_publication
    from daily_agent.production_revision import reserve_generation,recover_generation,run_generation as revision_run,prepare_handoff as revision_prepare
    from daily_agent.pipeline import run_pipeline
    from daily_agent.scheduling import repair_schedule_state
    root,jid,scope,oldday,_=release(scoped);fake_executing(root)
    actions=[lambda:run_generation(root,oldday,900),lambda:generate(root,oldday),lambda:prepare_handoff(root,oldday,'fixture'),
             lambda:record_transition(root,oldday,'begin'),lambda:reconcile_publication(root,oldday),
             lambda:reserve_generation(root,oldday,'old'),lambda:recover_generation(root,oldday,'old'),
             lambda:revision_run(root,oldday,'old'),lambda:revision_prepare(root,oldday,'old'),
             lambda:run_pipeline(root,oldday),lambda:repair_schedule_state(root,oldday,reset_budget=True)]
    for action in actions:
        with pytest.raises((StateCorrupt,ValueError)):action()


@pytest.mark.parametrize('name',['data/state/cloud-budget/{day}.json','reports/daily-agent-{day}.md','data/selected/selected-{day}.json'])
def test_prefixed_old_paths_and_direct_old_budget_writes_block(scoped,name):
    root,jid,scope,oldday,_=release(scoped);fake_executing(root)
    target=root/name.format(day=oldday)
    with pytest.raises(StateCorrupt,match='Historical'):atomic_bytes(target,b'overwrite')
    with pytest.raises(StateCorrupt,match='Historical'):atomic_json(root/'data/state/cloud-generation.json',{'date':str(oldday),'running':True})


def test_scope_deadline_cannot_be_extended_or_admitted_after_expiry(scoped):
    from daily_agent.parent_writer import request
    from daily_agent.durable_boundary import arm_boundary
    root,jid,scope,oldday,_=release(scoped);fake_executing(root)
    too_late=(datetime.fromisoformat(scope['deadline'])+timedelta(hours=1)).isoformat()
    with pytest.raises(StateCorrupt,match='deadline cannot exceed'):
        atomic_json(root/'data/state/fresh-budget.json',{'date':scope['issue_date'],'deadline':too_late})
    path=root/'.daily-agent-recovery-scope.json';v=json.loads(path.read_text());v['scope']['deadline']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();write(path,v)
    for action in [lambda:assert_issue_allowed(root,scope['issue_date']),lambda:request(root,'new prompt',10),lambda:arm_boundary(root,'new','owner')]:
        with pytest.raises(StateCorrupt,match='deadline expired'):action()


def test_historical_retention_is_noop(scoped):
    from daily_agent.cloud_workflow import _config
    from daily_agent.storage import cleanup_retention,_delete_old_files,_delete_old_date_dirs
    root,jid,scope,oldday,_=release(scoped);fake_executing(root)
    old=root/f'reports/daily-agent-{oldday}.md';old.parent.mkdir(exist_ok=True);old.write_text('retain')
    dated=root/f'data/pdfs/{oldday}';dated.mkdir(parents=True);(dated/'paper.pdf').write_bytes(b'original')
    os.utime(old,(0,0))
    cleanup_retention(_config(root),today=date.today()+timedelta(days=999))
    _delete_old_files([old],date.today()+timedelta(days=999));_delete_old_date_dirs([dated],date.today()+timedelta(days=999))
    assert old.read_text()=='retain' and (dated/'paper.pdf').read_bytes()==b'original'


def test_forged_complete_or_stopped_disposition_cannot_release_unknown_claim(scoped):
    root,jid,scope,review,*_=scoped
    for outcome in ['owner_stopped','worker_completed']:
        changed=deepcopy(review);changed['claims'][0]['outcome']=outcome
        with pytest.raises(recovery.RecoveryBlocked):recovery.release_recovery(root,changed)
        assert (root/recovery.FENCE).exists()


def test_reviewed_fence_survives_complete_snapshot(scoped,tmp_path):
    from daily_agent.state_archive import snapshot
    import zipfile
    root,jid,scope,*_=release(scoped)
    saved=snapshot(root,tmp_path/'retained.zip')
    with zipfile.ZipFile(saved['archive']) as archive:
        assert '.daily-agent-recovery-reviewed.json' in archive.namelist()
        assert '.daily-agent-recovery-scope.json' in archive.namelist()
        assert f'{quarantine.DIRECTORY}/{jid}.json' in archive.namelist()


def test_scope_and_quarantine_survive_multiple_restored_heads(scoped):
    root,jid,scope,review,*_=scoped
    original_fence=json.loads((root/recovery.FENCE).read_text())
    recovery.release_recovery(root,review)
    initial_scope=(root/'.daily-agent-recovery-scope.json').read_bytes()
    for version in [1,2]:
        fresh=deepcopy(original_fence);fresh['receipts']['state']['version']=version
        fresh['receipts']['state']['file_id']=f'fixture_state_{version}'
        write(root/recovery.FENCE,fresh)
        assert quarantine.validate_quarantine(root,jid,scope)['old_owner_status']=='unknown'
        new_review=deepcopy(review);new_review['receipts']=fresh['receipts']
        recovery.release_recovery(root,new_review)
        assert (root/'.daily-agent-recovery-scope.json').read_bytes()==initial_scope
        assert quarantine.validate_quarantine(root,jid,scope)['old_owner_status']=='unknown'
    assert len(list((root/'data/recovery/reviewed-fences').glob('*.json')))==2
