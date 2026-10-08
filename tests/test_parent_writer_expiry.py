from datetime import datetime,timezone,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import pytest
from daily_agent.cloud_workflow import init_profile,_config,run_generation
from daily_agent.parent_writer import request,PendingResponse,ExpiredResponse,retry_expired,claim,import_response,pending

@pytest.fixture
def issue(tmp_path,monkeypatch):
    root=tmp_path/'cloud';init_profile(Path(__file__).resolve().parents[1],root,'codex')
    day=datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=1)
    monkeypatch.setattr('daily_agent.workflow_runtime.run_process',lambda *a,**k:75)
    assert run_generation(root,day,30)==75
    budget_path=_config(root).state_dir/f'cloud-generation-budgets/{day}.json'
    value=json.loads(budget_path.read_text());value['deadline']=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    budget_path.write_text(json.dumps(value))
    return root,day,budget_path

def old_job(root,prompt='immutable evidence'):
    with pytest.raises(PendingResponse) as caught:request(root,prompt,30)
    job_id=caught.value.job_id
    lease=claim(root,job_id,'old-reader')
    folder=root/'data/writer-queue'
    for suffix in ('.job.json','.claim.json'):
        path=folder/f'{job_id}{suffix}';value=json.loads(path.read_text())
        value['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();path.write_text(json.dumps(value))
    return job_id,lease

def test_expired_retry_preserves_old_evidence_and_frozen_budget(issue):
    root,day,budget_path=issue;old,lease=old_job(root);folder=root/'data/writer-queue'
    original_job=(folder/f'{old}.job.json').read_bytes();original_claim=(folder/f'{old}.claim.json').read_bytes()
    before=json.loads(budget_path.read_text())
    with pytest.raises(ExpiredResponse):request(root,'immutable evidence',30)
    assert pending(root)==[] and pending(root,include_expired=True)[0]['expired']
    new=retry_expired(root,old,day,'Explicit recovery in a later issue')
    assert new['job_id']!=old and new['retry']['previous_job_id']==old
    assert (folder/f'{old}.job.json').read_bytes()==original_job
    assert (folder/f'{old}.claim.json').read_bytes()==original_claim
    after=json.loads(budget_path.read_text())
    assert {k:v for k,v in after.items() if k!='writer_job_retries'}==before
    assert datetime.fromisoformat(new['expires_at'])<=datetime.fromisoformat(before['deadline'])
    with pytest.raises(ValueError):import_response(root,old,{'ok':True},'old-reader',claim_token=lease['token'])
    with pytest.raises(ValueError):import_response(root,new['job_id'],{'ok':True},'new-reader')
    fresh=claim(root,new['job_id'],'new-reader')
    assert fresh['token']!=lease['token'] and fresh['generation']>lease['generation']
    import_response(root,new['job_id'],{'ok':True},'new-reader',claim_token=fresh['token'])
    assert request(root,'immutable evidence',30)=={'ok':True}
    assert pending(root,include_expired=True)==[]

def test_retry_refuses_expired_issue_and_never_renews_automatically(issue):
    root,day,budget_path=issue;old,_=old_job(root)
    value=json.loads(budget_path.read_text());value['deadline']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();budget_path.write_text(json.dumps(value))
    before=budget_path.read_bytes()
    with pytest.raises(ValueError):retry_expired(root,old,day,'too late')
    assert budget_path.read_bytes()==before
    with pytest.raises(ExpiredResponse):request(root,'immutable evidence',30)

def test_retry_per_issue_cap_and_live_job_rejection(issue):
    root,day,budget_path=issue;current,_=old_job(root)
    for index in range(3):
        new=retry_expired(root,current,day,'bounded retry')
        with pytest.raises(ValueError):retry_expired(root,new['job_id'],day,'not expired')
        current=new['job_id'];path=root/'data/writer-queue'/f'{current}.job.json'
        value=json.loads(path.read_text());value['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();path.write_text(json.dumps(value))
    with pytest.raises(ValueError):retry_expired(root,current,day,'over budget')
    assert len(json.loads(budget_path.read_text())['writer_job_retries'])==3
