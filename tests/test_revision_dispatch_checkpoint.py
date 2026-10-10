from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
import pytest
from daily_agent import production_revision as rev, workflow_runtime, batch_dispatch
from daily_agent.workflow_state import read_json, atomic_json
from test_production_revision import parent, args
from test_cloud_site_delivery import site
from test_cloud_html_handoff import report, DAY
from test_dispatch_checkpoints import rows
from test_revision_presentation import display
from test_paper_visual_assets import example


def test_revision_worker_settlement_checkpoint_blocks_expired_relaunch(parent,monkeypatch,capsys):
    root,_=parent;c=rev.authorize_revision(root,DAY,**args(parent));rid=c['revision_id'];folder=rev.issue_dir(root,DAY,rid)
    jobs=rows();jobs[-1]['status']='expired'
    def generate(*a,**k):raise batch_dispatch.PendingBatchResponse('science-batch',jobs)
    monkeypatch.setattr(rev,'generate',generate);launches=[]
    def process(command,root,timeout,**callbacks):
        launches.append(command);callbacks['on_start']({'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline'})
        out=io.StringIO()
        with redirect_stdout(out):
            rc=rev.main(['worker','--root',str(root),'--date',str(DAY),'--revision-id',rid])
        path=callbacks['log_prefix'].with_suffix('.out.log');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(out.getvalue())
        return rc
    monkeypatch.setattr(workflow_runtime,'run_process',process)
    assert rev.run_generation(root,DAY,rid)==75
    state=read_json(folder/'generation.json');assert state['checkpoint']['jobs']==jobs
    before=(folder/'budget.json').read_bytes()
    blocked=rev.run_generation(root,DAY,rid)
    assert blocked['auto_resume'] is False and blocked['state']=='blocked_expired_parent_writer'
    assert blocked['job_id']==jobs[-1]['job_id'] and len(launches)==1
    assert (folder/'budget.json').read_bytes()==before
    with pytest.raises(rev.ScienceRecoveryRequired):rev.reserve_generation(root,DAY,rid)
    assert rev.main(['generate','--root',str(root),'--date',str(DAY),'--revision-id',rid])==78
    assert json.loads(capsys.readouterr().out)['auto_resume'] is False


def test_authorized_validated_presentation_transition_does_not_resume_expired_science(display):
    root,c,folder,*_=display;rid=c['revision_id'];jobs=rows();jobs[0]['status']='expired'
    checkpoint=batch_dispatch.validate_response_payload(batch_dispatch.response_payload(
        batch_dispatch.PendingBatchResponse('old-science-batch',jobs)),DAY)
    atomic_json(folder/'generation.json',{'revision_id':rid,'running':False,'checkpoint':checkpoint})
    launch=rev.reserve_generation(root,DAY,rid)
    assert launch['attempt_id']  # presentation-only checkpoint already exists and is fully validated
    rev.settle_generation(root,DAY,rid,launch['attempt_id'],75,0,expected_namespace=launch['namespace'])


def test_old_no_presentation_policy_cannot_use_matching_checkpoint_to_bypass_stop(parent):
    root,_=parent;c=rev.authorize_revision(root,DAY,**args(parent));rid=c['revision_id'];folder=rev.issue_dir(root,DAY,rid)
    jobs=rows();jobs[0]['status']='expired'
    checkpoint=batch_dispatch.validate_response_payload(batch_dispatch.response_payload(
        batch_dispatch.PendingBatchResponse('batch',jobs)),DAY)
    atomic_json(folder/'generation.json',{'revision_id':rid,'running':False,'checkpoint':checkpoint})
    # The old policy never authorized a transition into the presentation worker.
    carried,proofs=rev._committed_presentation_rows(root,DAY,rid)
    payload={'schema':1,'state':'awaiting_presentation','revision_id':rid,
             'contract_sha256':rev.digest(c),'approval':carried,'science':proofs}
    rev._presentation_write(folder/'presentation/checkpoint.json',payload)
    assert rev._presentation_checkpoint(root,DAY,rid)==payload
    with pytest.raises(rev.ScienceRecoveryRequired):rev.reserve_generation(root,DAY,rid)


def test_revision_expired_stop_intent_precedes_budget_settlement(parent,monkeypatch):
    root,_=parent;c=rev.authorize_revision(root,DAY,**args(parent));rid=c['revision_id'];folder=rev.issue_dir(root,DAY,rid)
    jobs=rows();jobs[0]['status']='expired';payload={**batch_dispatch.response_payload(
        batch_dispatch.PendingBatchResponse('batch',jobs)),'issue_date':str(DAY),'revision_id':rid}
    def process(command,root,timeout,**callbacks):
        callbacks['on_start']({'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline'})
        path=callbacks['log_prefix'].with_suffix('.out.log');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(payload)+'\n')
        return 75
    monkeypatch.setattr(workflow_runtime,'run_process',process);original=rev.atomic_json
    def crash(path,value):
        if path==folder/'budget.json' and value.get('last_returncode')==75:raise OSError('settlement crash')
        return original(path,value)
    monkeypatch.setattr(rev,'atomic_json',crash)
    with pytest.raises(OSError):rev.run_generation(root,DAY,rid)
    state=read_json(folder/'generation.json');budget=read_json(folder/'budget.json')
    assert budget.get('active_attempt_id') and state['running']
    assert state['checkpoint']['auto_resume'] is False
