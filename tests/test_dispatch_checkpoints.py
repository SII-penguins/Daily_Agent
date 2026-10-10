from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import os
import pytest
from daily_agent import cloud_workflow as cloud, workflow_runtime, batch_dispatch
from daily_agent.workflow_state import read_json
from test_cloud_attempt_fencing import config, DAY, paths


def rows(count=2):
    result=[]
    for index in range(count):
        key=f'material-{index}';op=[key,'native','chunk:c1',0]
        result.append({'material':key,'job_id':f'{index+1:064x}','status':'pending','operation':op,
            'role':'reading','expires_at':'2026-10-10T06:00:00+00:00','claim':None,
            'bindings':[{'material':key,'operation':op}]})
    return result


@pytest.mark.parametrize('count,expired',[(2,False),(2,True),(208,False)])
def test_worker_stdout_supervisor_checkpoint_and_prepare_preserve_full_set(config,monkeypatch,capsys,count,expired):
    jobs=rows(count)
    if expired:jobs[-1]['status']='expired'
    pending=batch_dispatch.PendingBatchResponse('fixed-batch',jobs)
    def generate(*a,**k):raise pending
    monkeypatch.setattr(cloud,'generate',generate)
    def process(command,root,timeout,**callbacks):
        callbacks['on_start']({'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline'})
        stream=io.StringIO()
        with redirect_stdout(stream):
            rc=cloud.main(['generate','--root',str(root),'--date',str(DAY)])
        log=callbacks['log_prefix'].with_suffix('.out.log');log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a') as handle:handle.write(stream.getvalue())
        return rc
    monkeypatch.setattr(workflow_runtime,'run_process',process)
    assert cloud.main(['prepare','--root',str(config.root),'--date',str(DAY),'--conversation','verified'])==75
    output=json.loads(capsys.readouterr().out)
    budget,state=(read_json(p) for p in paths(config))
    assert output['jobs']==state['checkpoint']['jobs']==jobs
    assert output['job_id']==(jobs[-1]['job_id'] if expired else jobs[0]['job_id'])
    assert budget['failures']==0 and budget['resumes']==1


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(issue_date='2026-10-11'),
    lambda p:p['jobs'][0].update(job_id='invalid'),
    lambda p:p['jobs'][0].update(status='approved'),
    lambda p:p['jobs'][0].update(claim={'token':'must not expose'}),
    lambda p:p.update(jobs=rows(batch_dispatch.MAX_WAIT_JOBS+1)),
    lambda p:p.update(extra='x'*batch_dispatch.MAX_CHECKPOINT_BYTES),
    lambda p:p.update(job_id='f'*64),
    lambda p:p.update(state='expired_parent_writer'),
])
def test_bad_or_oversized_checkpoint_is_not_persisted_as_valid(mutation):
    payload={**batch_dispatch.response_payload(batch_dispatch.PendingBatchResponse('batch',rows())), 'issue_date':str(DAY)}
    assert batch_dispatch.validate_response_payload(payload,DAY)
    mutation(payload)
    assert batch_dispatch.validate_response_payload(payload,DAY) is None


def test_queue_busy_legacy_diagnostic_is_explicit_and_valid():
    exc=batch_dispatch.QueueObservationBusy('batch','a'*64)
    payload=batch_dispatch.response_payload(exc)
    assert batch_dispatch.validate_response_payload(payload,DAY)['reason']=='queue_observation_busy'


def test_expired_checkpoint_stops_next_supervisor_without_budget_or_launch(config,monkeypatch,capsys):
    from daily_agent.workflow_state import atomic_json
    pending=rows();pending[1]['status']='expired'
    checkpoint=batch_dispatch.validate_response_payload(batch_dispatch.response_payload(
        batch_dispatch.PendingBatchResponse('batch',pending)),DAY)
    atomic_json(paths(config)[1],{'date':str(DAY),'running':False,'checkpoint':checkpoint})
    before=paths(config)[1].read_bytes()
    monkeypatch.setattr(workflow_runtime,'run_process',lambda *a,**k:pytest.fail('expired job relaunched'))
    assert cloud.run_generation(config.root,DAY,30)==78
    assert paths(config)[1].read_bytes()==before and not paths(config)[0].exists()
    assert cloud.main(['prepare','--root',str(config.root),'--date',str(DAY),'--conversation','verified'])==78
    output=json.loads(capsys.readouterr().out)
    assert output['auto_resume'] is False and output['state']=='blocked_expired_parent_writer'
    assert output['jobs']==pending


def test_non_ascii_checkpoint_limit_matches_actual_cli_bytes():
    jobs=rows(208)
    for row in jobs:
        row['claim']={'worker_id':'中文'*110,'generation':1,'expires_at':row['expires_at'],'active':True}
    payload={**batch_dispatch.response_payload(batch_dispatch.PendingBatchResponse('batch',jobs)), 'issue_date':str(DAY)}
    assert len(json.dumps(payload).encode()) < batch_dispatch.MAX_CHECKPOINT_BYTES
    assert batch_dispatch.validate_response_payload(payload,DAY)['jobs']==jobs
    for row in jobs:row['claim']['worker_id']='中文'*300
    assert len(json.dumps(payload).encode()) > batch_dispatch.MAX_CHECKPOINT_BYTES
    assert batch_dispatch.validate_response_payload(payload,DAY) is None


def test_expired_stop_intent_survives_crash_before_budget_settlement(config,monkeypatch):
    from daily_agent.workflow_state import atomic_json
    jobs=rows();jobs[0]['status']='expired';payload={**batch_dispatch.response_payload(
        batch_dispatch.PendingBatchResponse('batch',jobs)),'issue_date':str(DAY)}
    def process(command,root,timeout,**callbacks):
        callbacks['on_start']({'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline'})
        log=callbacks['log_prefix'].with_suffix('.out.log');log.parent.mkdir(parents=True,exist_ok=True);log.write_text(json.dumps(payload)+'\n')
        return 75
    monkeypatch.setattr(workflow_runtime,'run_process',process)
    original=cloud.atomic_json
    def crash(path,value):
        if path==paths(config)[0] and value.get('last_returncode')==75:raise OSError('settlement crash')
        return original(path,value)
    monkeypatch.setattr(cloud,'atomic_json',crash)
    with pytest.raises(OSError):cloud.run_generation(config.root,DAY,30)
    budget,state=(read_json(p) for p in paths(config))
    assert budget.get('active_attempt_id') and state['running']
    assert state['checkpoint']['auto_resume'] is False


def test_queue_capacity_stdout_checkpoint_prepare_is_75_without_failure(config,monkeypatch,capsys):
    from daily_agent.parent_writer import QueueCapacityPending
    def generate(*a,**k):raise QueueCapacityPending()
    monkeypatch.setattr(cloud,'generate',generate)
    def process(command,root,timeout,**callbacks):
        callbacks['on_start']({'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline'})
        stream=io.StringIO()
        with redirect_stdout(stream):rc=cloud.main(['generate','--root',str(root),'--date',str(DAY)])
        log=callbacks['log_prefix'].with_suffix('.out.log');log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a') as handle:handle.write(stream.getvalue())
        return rc
    monkeypatch.setattr(workflow_runtime,'run_process',process)
    assert cloud.main(['prepare','--root',str(config.root),'--date',str(DAY),'--conversation','verified'])==75
    output=json.loads(capsys.readouterr().out)
    budget,state=(read_json(p) for p in paths(config))
    assert output['reason']==state['checkpoint']['reason']=='queue_capacity'
    assert output['job_id'] is None and budget['failures']==0
