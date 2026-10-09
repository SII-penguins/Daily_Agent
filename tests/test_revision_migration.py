"""Offline-only transaction fencing/migration. Never touches the production root."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import pytest
from test_cloud_html_handoff import report,DAY
from test_cloud_site_delivery import site
from test_production_revision import parent,authorized,fixture_supervisor,make_repo
from daily_agent import production_revision as r
from daily_agent.workflow_state import atomic_json,read_json,StateCorrupt,exclusive_lock,WorkflowBusy
from daily_agent.incremental_issue import source_version
from daily_agent.workflow_runtime import CleanupPending


def fence_inputs(root,c,monkeypatch,*,expire=True):
    rid=c['revision_id']; launch=r.reserve_generation(root,DAY,rid,900)
    folder=r.issue_dir(root,DAY,rid)
    b=read_json(folder/'budget.json')
    when=datetime.now(timezone.utc)-timedelta(seconds=1200 if expire else 10)
    b['active_started_at']=when.isoformat();atomic_json(folder/'budget.json',b)
    for path in r._migration_locks(root,DAY,rid).values():
        path.parent.mkdir(parents=True,exist_ok=True);path.touch(exist_ok=True)
    e=r.migration_evidence(root,DAY,rid)
    kwargs={'expected_evidence_sha':e['sha256'],
            'authorization':{'source':'offline:root-authorized-fence','text':'Retire expired attempt; preserve all spent resources','authorized_at':datetime.now(timezone.utc).isoformat()},
            'expected_source_version':source_version(),
            'shared_lock_proof':{'source':'offline temporary shared-lock fixture','shared_locking_verified':True,'locks':e['payload']['locks']}}
    return rid,launch,kwargs,e


def test_expired_migration_inherits_budget_and_candidates(authorized,monkeypatch):
    root,c,_=authorized;rid,launch,k,e=fence_inputs(root,c,monkeypatch)
    old_bytes=(r.issue_dir(root,DAY,rid)/'budget.json').read_bytes()
    new=r.retire_and_replace(root,DAY,rid,**k);newid=new['revision_id']
    assert newid!=rid and new['logical_issue_id']==rid and new['candidates']==c['candidates']
    assert new['carry_forward']==c['carry_forward'] and new['policy']==c['policy']
    b=r._read_budget(root,DAY,newid)
    assert b['runtime_seconds']>=1200 and b['forfeited_seconds']>=1200
    assert b['failures']==1 and b['resumes']==1 and b['migration_accounting']['measured'] is False
    assert (r.issue_dir(root,DAY,rid)/'budget.json').read_bytes()==old_bytes
    # Exact retry is read-only after completed migration, even target progresses.
    reserve=r.reserve_generation(root,DAY,newid,20)
    assert r.retire_and_replace(root,DAY,rid,**k)==new
    assert r._read_budget(root,DAY,newid)['resumes']==2
    for call in [lambda:r.reserve_generation(root,DAY,rid),lambda:r.freeze_completed(root,DAY,rid),
                 lambda:r.seal_ready(root,DAY,rid,r.carry_forward_rows(root,DAY,rid)),
                 lambda:r.prepare_handoff(root,DAY,rid),lambda:r.record_transition(root,DAY,rid,'begin')]:
        with pytest.raises(StateCorrupt,match='migration-owned'):call()


def test_no_migration_before_expiry_bad_proof_or_evidence_drift(authorized,monkeypatch):
    root,c,_=authorized;rid,launch,k,e=fence_inputs(root,c,monkeypatch,expire=False)
    with pytest.raises(ValueError,match='not expired'):r.retire_and_replace(root,DAY,rid,**k)
    bad=deepcopy(k);bad['shared_lock_proof']['shared_locking_verified']=False
    with pytest.raises(ValueError,match='probe'):r.retire_and_replace(root,DAY,rid,**bad)
    p=r.issue_dir(root,DAY,rid)/'generation.json';s=read_json(p);s['heartbeat_at']='new evidence';atomic_json(p,s)
    with pytest.raises(StateCorrupt,match='evidence changed'):r.retire_and_replace(root,DAY,rid,**k)
    assert not (p.parent/'migration-snapshot.json').exists()


def test_busy_real_lock_blocks_transaction(authorized,monkeypatch):
    root,c,_=authorized;rid,launch,k,e=fence_inputs(root,c,monkeypatch)
    with exclusive_lock(r._migration_locks(root,DAY,rid)['pipeline']):
        with pytest.raises(WorkflowBusy):r.retire_and_replace(root,DAY,rid,**k)
    assert not (r.issue_dir(root,DAY,rid)/'migration-snapshot.json').exists()


@pytest.mark.parametrize('crash_at',['retired.json','initialized.json'])
def test_half_write_retries_only_snapshot_despite_old_parent_late_writes(authorized,monkeypatch,crash_at):
    root,c,_=authorized;rid,launch,k,e=fence_inputs(root,c,monkeypatch)
    folder=r.issue_dir(root,DAY,rid);original=r._immutable
    def fault(path,value):
        if path.name==crash_at and (crash_at!='initialized.json' or path.parent!=folder): raise OSError('offline crash')
        return original(path,value)
    monkeypatch.setattr(r,'_immutable',fault)
    with pytest.raises(OSError,match='offline crash'):r.retire_and_replace(root,DAY,rid,**k)
    snap=r._read_migration_snapshot(folder);newid=snap['replacement_id']
    with pytest.raises(StateCorrupt,match='migration-owned'):r.reserve_generation(root,DAY,rid)
    # Simulate the old, already-loaded supervisor's unguarded late callback.
    b=read_json(folder/'budget.json');b['runtime_seconds']=0;b['resumes']=999;atomic_json(folder/'budget.json',b)
    atomic_json(folder/'generation.json',{'late_old_parent':True})
    monkeypatch.setattr(r,'_immutable',original)
    new=r.retire_and_replace(root,DAY,rid,**k)
    b=r._read_budget(root,DAY,newid)
    assert new['revision_id']==newid and b['resumes']==1 and b['runtime_seconds']==snap['forfeited_seconds']
    assert b['failures']==1


def test_unknown_execution_never_becomes_fresh_ledger(authorized,monkeypatch):
    root,c,_=authorized;rid,launch,k,e=fence_inputs(root,c,monkeypatch)
    p=r.issue_dir(root,DAY,rid)/'incremental/batch-execution/unknown/journal.json'
    atomic_json(p,{'not':'supported'})
    k['expected_evidence_sha']=r.migration_evidence(root,DAY,rid)['sha256']
    with pytest.raises(StateCorrupt,match='Unknown nonempty'):r.retire_and_replace(root,DAY,rid,**k)
    assert not (p.parents[3]/'migration-snapshot.json').exists()


def test_worker_token_namespace_and_pid_reuse_are_all_bound(authorized,monkeypatch):
    root,c,_=authorized;rid=c['revision_id'];launch=r.reserve_generation(root,DAY,rid)
    fixture_supervisor(root,rid,launch)
    with pytest.raises(StateCorrupt):r.assert_active_generation(root,DAY,rid)
    with pytest.raises(StateCorrupt):r.assert_active_generation(root,DAY,rid,expected_attempt='wrong',expected_namespace=launch['namespace'])
    with pytest.raises(StateCorrupt):r.assert_active_generation(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace='f'*64)
    r.assert_active_generation(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
    monkeypatch.setattr('daily_agent.workflow_runtime.process_namespace',lambda:'f'*64)
    with pytest.raises(StateCorrupt):r.assert_active_generation(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])


def test_stale_started_tick_and_final_cannot_mutate_current_attempt(authorized):
    root,c,_=authorized;rid=c['revision_id'];launch=r.reserve_generation(root,DAY,rid)
    folder=r.issue_dir(root,DAY,rid)
    before={p:p.read_bytes() for p in (folder/'budget.json',folder/'generation.json')}
    for f in [lambda:r._generation_cas(root,DAY,rid,'stale',launch['namespace'],{'child_identity':{'pid':5,'pgid':5}}),
              lambda:r._generation_cas(root,DAY,rid,'stale',launch['namespace'],{'heartbeat_at':'late'}),
              lambda:r.settle_generation(root,DAY,rid,'stale',0,1,expected_namespace=launch['namespace'])]:
        with pytest.raises(StateCorrupt):f()
    assert all(p.read_bytes()==v for p,v in before.items())


@pytest.mark.parametrize('callback',[True,False])
def test_cleanup_pending_never_settles_even_when_first_started_cas_fails(authorized,monkeypatch,callback):
    root,c,_=authorized;rid=c['revision_id']
    def fake_run(*a,**kw):
        if callback:
            try:kw['on_start']({'pid':5,'pgid':5,'description':None})
            except OSError:pass
        raise CleanupPending('offline unknown live child')
    monkeypatch.setattr(r,'_generation_cas',lambda *a,**k:(_ for _ in ()).throw(OSError('offline write failed')))
    monkeypatch.setattr('daily_agent.workflow_runtime.run_process',fake_run)
    with pytest.raises(CleanupPending):r.run_generation(root,DAY,rid)
    b=r._read_budget(root,DAY,rid)
    assert b['active_attempt_id'] and b['resumes']==1 and b['failures']==0


def test_recovery_checks_state_namespace_before_signal(authorized,monkeypatch):
    root,c,_=authorized;rid=c['revision_id'];launch=r.reserve_generation(root,DAY,rid)
    fixture_supervisor(root,rid,launch)
    p=r.issue_dir(root,DAY,rid)/'generation.json';s=read_json(p);s['namespace']='f'*64;atomic_json(p,s)
    monkeypatch.setattr('daily_agent.workflow_runtime.stop_verified_orphan',lambda *a,**k:pytest.fail('must not signal'))
    with pytest.raises(StateCorrupt):r.recover_generation(root,DAY,rid)
