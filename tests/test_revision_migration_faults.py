"""Independent temporary-root crash tests; no production state or model use."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,os,subprocess,sys
from pathlib import Path
import pytest
from test_production_revision import parent,site,report,DAY,args,make_repo
from daily_agent import production_revision as revision
from daily_agent import incremental_issue
from daily_agent.batch_execution import Execution,candidate,existing_review_validator,BudgetExhausted,digest as bdigest
from daily_agent.cloud_workflow import _config
from daily_agent.storage import load_material_library,write_material_library
from daily_agent.workflow_state import read_json,atomic_json

@pytest.fixture
def prepared(parent,monkeypatch,tmp_path):
    root,_=parent;config=_config(root)
    monkeypatch.setattr(incremental_issue,'source_version',lambda:'e'*64)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    a,_,_=make_repo('github:fixture/completed');b,_,_=make_repo('github:fixture/pending')
    library=load_material_library(config);library.update({a.key:a,b.key:b});write_material_library(config,library)
    c=revision.authorize_revision(root,DAY,**args(parent));rid=c['revision_id'];folder=revision.issue_dir(root,DAY,rid)
    assert {a.key,b.key}<={r['key'] for r in c['candidates']}
    start=revision._now();clock={'now':start}
    monkeypatch.setattr(revision,'_now',lambda:clock['now'])
    launch=revision.reserve_generation(root,DAY,rid,900)
    # Faithful old loaded-source metadata: PID-only namespace, no current guard token.
    budget=read_json(folder/'budget.json');budget.pop('active_namespace',None);atomic_json(folder/'budget.json',budget)
    state=read_json(folder/'generation.json');state.pop('namespace',None)
    state['child_identity']={'pid':5,'pgid':5,'description':'unreachable original namespace worker'}
    atomic_json(folder/'generation.json',state)
    payload={'id':f'{rid}:batch:0','selected':[a.to_dict(),b.to_dict()]}
    revision._immutable(folder/'batches/0.json',{'payload':payload,'sha256':revision.digest(payload)})
    values=[a.to_dict(),b.to_dict()]
    revision._immutable(folder/'batches/0.enriched.json',{'payload':values,'sha256':revision.digest(values)})
    inputs=tmp_path/'ledger-inputs.json';inputs.write_text(json.dumps(values))
    program=r'''
import json,os,sys
from pathlib import Path
from daily_agent.cloud_workflow import _config
from daily_agent.models import MaterialRecord,EditorialDraft,EditorialReview
from daily_agent.editorial import REPO_FIELDS
from daily_agent.batch_execution import Execution,candidate,existing_review_validator
from daily_agent.incremental_issue import PROTOCOL
from daily_agent.parent_writer import request,PendingResponse
root,folder,rid,inputs=sys.argv[1:];cfg=_config(root)
a,b=[MaterialRecord.from_dict(r) for r in json.loads(Path(inputs).read_text())]
with Execution(Path(folder)/'incremental',rid+':batch:0',[candidate(a),candidate(b)],cfg,protocol=PROTOCOL) as ex:
 d=EditorialDraft(a.key,'repo',a.title,{f:'Concrete capability from fixture README' for f in REPO_FIELDS})
 review=EditorialReview(a.key,'PASS')
 sha=ex.prepare_completion(a.key,record=a.to_dict(),draft=d.to_dict(),review=review.to_dict(),science={},evidence={},asset_hashes={})
 ex.commit_completion(a.key,sha,validator=existing_review_validator(cfg))
 with ex.stage('primary_writer'):
  try:request(root,'offline pending repository prompt',20,stage='draft',execution=ex,operation=(b.key,'primary_writer','draft',0))
  except PendingResponse:os._exit(0)
 raise AssertionError('Expected fixture pending')
'''
    result=subprocess.run([sys.executable,'-c',program,str(root),str(folder),rid,str(inputs)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    ledger=folder/'incremental/batch-execution'/bdigest(f'{rid}:batch:0')
    before=read_json(ledger/'journal.json')['payload'];assert before['reservations'] and a.key in before['completed']
    for path in revision._migration_locks(root,DAY,rid).values():path.parent.mkdir(parents=True,exist_ok=True);path.touch(exist_ok=True)
    clock['now']=start+timedelta(seconds=930)
    e=revision.migration_evidence(root,DAY,rid)
    authorization={'source':'offline-fixture:explicit-recovery','text':'Recover conservatively without extra budget','authorized_at':clock['now'].isoformat()}
    kw={'expected_evidence_sha':e['sha256'],'authorization':authorization,'expected_source_version':'e'*64,
        'shared_lock_proof':{'source':'offline-only shared-lock fixture','shared_locking_verified':True,'locks':e['payload']['locks']}}
    return root,c,folder,a,b,clock,kw,before

@pytest.mark.parametrize('crash',['after_snapshot','after_replacement_budget'])
def test_crash_late_old_callbacks_never_reset_or_change_replacement(prepared,monkeypatch,crash):
    root,c,folder,a,b,clock,kw,before=prepared;rid=c['revision_id']
    old=revision._immutable;triggered=[]
    def fail(path,value):
        path=Path(path)
        hit=(crash=='after_snapshot' and path==folder/'retired.json') or (crash=='after_replacement_budget' and path.name=='initialized.json' and path.parent!=folder)
        if hit and not triggered:
            triggered.append(str(path));raise OSError('fixture crash boundary')
        return old(path,value)
    monkeypatch.setattr(revision,'_immutable',fail)
    with pytest.raises(OSError,match='fixture crash'):revision.retire_and_replace(root,DAY,rid,**kw)
    assert triggered
    snap=read_json(folder/'migration-snapshot.json')['payload'];replacement=snap['replacement_id']
    assert snap['process_death_claimed'] is False
    expected=snap['forfeited_seconds'];assert expected>=930
    # Simulate old in-memory source's unguarded late settlement/metadata writes.
    old_budget=read_json(folder/'budget.json');old_budget['runtime_seconds']=9999;old_budget['failures']=2
    for key in ('active_attempt_id','active_started_at','active_timeout_seconds'):old_budget.pop(key,None)
    old_budget['last_attempt_id']='late-old-write';atomic_json(folder/'budget.json',old_budget)
    atomic_json(folder/'generation.json',{'revision_id':rid,'running':True,'attempt_id':'late-old-write','child_identity':{'pid':5,'pgid':5}})
    clock['now']+=timedelta(seconds=1500)
    new=revision.retire_and_replace(root,DAY,rid,**kw)
    assert new['revision_id']==replacement and new['logical_issue_id']==rid
    assert new['candidates']==c['candidates'] and new['policy']==c['policy']
    target=revision.issue_dir(root,DAY,replacement)
    budget=revision._read_budget(root,DAY,replacement)
    assert budget['runtime_seconds']==expected and budget['forfeited_seconds']==expected
    assert budget['failures']==1 and budget['resumes']==1
    assert budget['deadline']==c['policy']['deadline'] and budget['max_runtime_seconds']==21600
    assert budget['migration_accounting']['measured'] is False
    new_ledger=target/'incremental/batch-execution'/bdigest(f'{rid}:batch:0')
    copied=read_json(new_ledger/'journal.json')['payload']
    assert copied['operations']==before['operations'] and copied['reservations']==before['reservations']
    assert copied['completed']==before['completed'] and copied['writer_circuit']==before['writer_circuit']
    jobs=list((root/'data/writer-queue').glob('*.job.json'));assert len(jobs)==1
    cfg=_config(root)
    with Execution(target/'incremental',f'{rid}:batch:0',[candidate(a),candidate(b)],cfg,protocol=incremental_issue.PROTOCOL) as ex:
        after=ex.snapshot();pool=after['pools']['primary_writer']
        assert pool['remaining']==0 and pool['forfeited']==before['pools']['primary_writer']['limit']
        assert not after['reservations'] and after['operations']==before['operations']
        assert ex.completed(a.key,validator=existing_review_validator(cfg)) is not None
        with pytest.raises(BudgetExhausted):
            with ex.stage('primary_writer'):pytest.fail('no fresh writer capacity')
    # A completed idempotent retry preserves now-progressed replacement budget/ledger.
    journal_bytes=(new_ledger/'journal.json').read_bytes();budget_bytes=(target/'budget.json').read_bytes()
    assert revision.retire_and_replace(root,DAY,rid,**kw)['revision_id']==replacement
    assert (new_ledger/'journal.json').read_bytes()==journal_bytes
    assert (target/'budget.json').read_bytes()==budget_bytes
    assert len(list((root/'data/writer-queue').glob('*.job.json')))==1
