"""Offline presentation state-machine tests, with real PDF crops and queue IO.
Science fixtures are separate from presentation tests; no model calls occur.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import pytest
from test_production_revision import parent,args,make_repo,fixture_supervisor
from test_cloud_site_delivery import site
from test_cloud_html_handoff import report,DAY
from test_paper_visual_assets import example
from daily_agent import production_revision as rev
from daily_agent import parent_writer as queue
from daily_agent.models import ApprovedItem
from daily_agent.workflow_state import StateCorrupt,read_json,atomic_json


@pytest.fixture
def display(parent,example,monkeypatch):
    root,_=parent
    c=rev.authorize_revision(root,DAY,**args(parent),presentation_policy=rev.PRESENTATION_POLICY)
    rid=c['revision_id'];folder=rev.issue_dir(root,DAY,rid)
    material,_=example
    selection=deepcopy(material.raw.pop('paper_visual_selection'))
    selection.update(selection_policy_version=2,experiment_plot_coverage={
        'reviewed':True,'available':False,'key_plot_numbers':[],'absence_reason':'Only a table in offline fixture'})
    material.reading={'visual':{'notes':[{'page':1,'tables':['Table 1'],'figures':[],'formulas':[]}]}}
    row=ApprovedItem(key=material.key,item_type='paper',title=material.title,source=material.source,
                     url=material.url,final_fields={},material=material).to_dict()
    rows=rev.carry_forward_rows(root,DAY,rid)+[row]
    proofs={material.key:{'batch_id':rid+':batch:0','completion_sha256':'a'*64}}
    monkeypatch.setattr(rev,'_committed_presentation_rows',lambda *a:(deepcopy(rows),deepcopy(proofs)))
    monkeypatch.setattr(rev,'_check_rows',lambda *a:None)
    rev.freeze_completed(root,DAY,rid)
    return root,c,folder,rows,selection


def resume(display):
    root,c,*_=display;rid=c['revision_id']
    launch=rev.reserve_generation(root,DAY,rid,900);fixture_supervisor(root,rid,launch)
    try:
        return rev.generate(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
    finally:
        rev.settle_generation(root,DAY,rid,launch['attempt_id'],75,.01,expected_namespace=launch['namespace'])


def answer(display,job,selection=None):
    root,_,_,_,valid=display
    lease=queue.claim(root,job,'offline-selector',600)
    return queue.import_response(root,job,selection or valid,'offline-selector',claim_token=lease['token'])


def test_checkpoint_pending_restart_no_science_and_unique_seal(display,monkeypatch):
    root,c,folder,rows,selection=display
    monkeypatch.setattr('daily_agent.pipeline._process_incremental_batch',lambda *a,**k:pytest.fail('Science must not rerun'))
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    job=caught.value.job_id
    assert not (folder/'ready.json').exists()
    checkpoint=(folder/'presentation/checkpoint.json').read_bytes()
    ops=list((folder/'presentation/operations').glob('*.json'));assert len(ops)==1
    operation=ops[0].read_bytes()
    with pytest.raises(queue.PendingResponse) as again:resume(display)
    assert again.value.job_id==job and ops[0].read_bytes()==operation
    answer(display,job)
    ready=resume(display)
    assert ready['approval'][:-1]==rows[:-1]
    assert ready['approval'][-1]['material']['reading']['paper_visual_assets']['status']=='ready'
    assert checkpoint==(folder/'presentation/checkpoint.json').read_bytes()
    sealed=(folder/'ready.json').read_bytes()
    with pytest.raises(ValueError,match='Sealed'):resume(display)
    assert sealed==(folder/'ready.json').read_bytes()


def test_bad_crop_never_seals_and_bad_answer_cannot_be_replaced(display):
    _,_,folder,_,selection=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    bad=deepcopy(selection);bad['assets'][0]['bbox']=[0,0,999,999]
    answer(display,caught.value.job_id,bad)
    blocked=resume(display)
    assert blocked['state']=='presentation_blocked' and 'crop' in blocked['detail']
    assert not (folder/'ready.json').exists()
    with pytest.raises(ValueError):answer(display,caught.value.job_id,selection)
    assert len(list((folder/'presentation/operations').glob('*.json')))==1


def test_expiry_and_pending_wall_cannot_reset(display,monkeypatch):
    root,c,folder,*_=display
    with pytest.raises(queue.PendingResponse):resume(display)
    op=rev._presentation_read(next((folder/'presentation/operations').glob('*.json')))
    created=rev._stamp(op['started_at'])
    monkeypatch.setattr(rev,'_now',lambda:created+timedelta(seconds=601))
    assert rev._presentation_charge(folder,c)==pytest.approx(601)
    from daily_agent.batch_execution import BudgetExhausted
    before=(folder/'budget.json').read_bytes()
    blocked=rev.run_generation(root,DAY,c['revision_id'])
    assert blocked['reason']=='selection_expired'
    assert before==(folder/'budget.json').read_bytes()
    assert not (folder/'ready.json').exists()
    assert len(list((folder/'presentation/operations').glob('*.json')))==1


def test_budget_rejects_before_new_operation(display):
    root,c,folder,*_=display
    b=rev._read_budget(root,DAY,c['revision_id']);b['runtime_seconds']=b['max_runtime_seconds']
    atomic_json(folder/'budget.json',b)
    with pytest.raises(ValueError,match='budget'):resume(display)
    assert not list((folder/'presentation/operations').glob('*.json'))


def test_bare_seal_and_scientific_mutation_rejected(display):
    root,c,folder,rows,_=display
    with pytest.raises(StateCorrupt,match='supervised'):rev.seal_ready(root,DAY,c['revision_id'],rows)
    altered=deepcopy(rows[-1]);altered['final_fields']['claim']='invented'
    with pytest.raises(StateCorrupt,match='scientific'):rev._validate_display(rows[-1],altered)


def test_queue_deadline_is_real_and_claim_clipped(display):
    root,c,folder,*_=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    op=rev._presentation_read(next((folder/'presentation/operations').glob('*.json')))
    job=read_json(root/'data/writer-queue'/(caught.value.job_id+'.job.json'))
    assert job['expires_at']==op['deadline']
    assert 590< (rev._stamp(job['expires_at'])-rev._stamp(job['created_at'])).total_seconds()<=600
    lease=queue.claim(root,caught.value.job_id,'selector',3600)
    assert lease['expires_at']==job['expires_at']


def test_last_repository_commits_then_checkpoint_not_ready(parent,monkeypatch):
    from daily_agent import pipeline
    from daily_agent.storage import load_material_library,write_material_library
    from daily_agent.batch_execution import Execution,candidate,existing_review_validator
    from daily_agent.incremental_issue import PROTOCOL
    root,_=parent;config=rev._config(root)
    r,d,review=make_repo('github:fixture/final')
    lib=load_material_library(config);lib[r.key]=r;write_material_library(config,lib)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    monkeypatch.setattr(pipeline,'build_shortlist',lambda config,records,day:list(records.values()))
    monkeypatch.setattr('daily_agent.cloud_cache.prepare_batch',lambda config,day,batch,enrich:batch)
    c=rev.authorize_revision(root,DAY,**args(parent),presentation_policy=rev.PRESENTATION_POLICY)
    rid=c['revision_id'];folder=rev.issue_dir(root,DAY,rid);calls=[]
    def process(config,day,batch,plan,batch_id,*a,**kw):
        with Execution(plan.folder,batch_id,[candidate(v) for v in batch],config,protocol=PROTOCOL) as ex:
            sha=ex.prepare_completion(r.key,record=batch[0].to_dict(),draft=d.to_dict(),review=review.to_dict(),science={},evidence={},asset_hashes={})
            ex.commit_completion(r.key,sha,validator=existing_review_validator(config))
        calls.append(sha);return [batch[0]],[d],[review]
    monkeypatch.setattr(pipeline,'_process_incremental_batch',process)
    run=(root,c,folder,None,None)
    cp=resume(run)
    assert cp['state']=='awaiting_presentation' and not (folder/'ready.json').exists()
    assert cp['science'][r.key]['completion_sha256']==calls[0]
    snapshot={p:p.read_bytes() for p in (folder/'incremental').rglob('*.json')}
    ready=resume(run)
    assert len(calls)==1 and ready['approval']==cp['approval']
    assert all(p.read_bytes()==value for p,value in snapshot.items())


def test_existing_legacy_queue_deadline_is_not_rewritten(display):
    root,c,folder,rows,_=display
    from daily_agent.paper_visual_assets import request_visual_selection
    from daily_agent.scheduling import _approved_items
    item=_approved_items([deepcopy(rows[-1])])[0]
    with pytest.raises(queue.PendingResponse) as caught:request_visual_selection(item.material,root)
    path=root/'data/writer-queue'/(caught.value.job_id+'.job.json');before=path.read_bytes()
    from daily_agent.batch_execution import ExecutionConflict
    blocked=resume(display)
    assert blocked['state']=='presentation_blocked' and 'deadline' in blocked['detail']
    assert path.read_bytes()==before and not (folder/'ready.json').exists()


def test_answer_without_real_claim_rejected(display):
    root,c,folder,_,selection=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    queue.import_response(root,caught.value.job_id,selection,'unclaimed-selector')
    blocked=resume(display)
    assert blocked['state']=='presentation_blocked' and 'provenance' in blocked['detail']
    assert not (folder/'ready.json').exists()


def test_tampered_crop_after_result_before_seal_rejected(display,monkeypatch):
    root,c,folder,*_=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    answer(display,caught.value.job_id)
    original=rev._seal_ready_unlocked
    def pause(*a,**kw):raise RuntimeError('offline crash before seal')
    monkeypatch.setattr(rev,'_seal_ready_unlocked',pause)
    with pytest.raises(RuntimeError,match='crash'):resume(display)
    assert not (folder/'ready.json').exists()
    result=rev._presentation_read(next((folder/'presentation/results').glob('*.json')))
    crop=Path(result['row']['material']['reading']['paper_visual_assets']['assets'][0]['path'])
    crop.write_bytes(b'corrupt')
    monkeypatch.setattr(rev,'_seal_ready_unlocked',original)
    blocked=resume(display)
    assert blocked['state']=='presentation_blocked' and 'crop' in blocked['detail']
    assert not (folder/'ready.json').exists()


def test_first_handoff_revalidates_crops_after_seal(display):
    root,c,folder,*_=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    answer(display,caught.value.job_id)
    ready=resume(display)
    crop=Path(ready['approval'][-1]['material']['reading']['paper_visual_assets']['assets'][0]['path'])
    crop.write_bytes(b'changed after seal')
    with pytest.raises(StateCorrupt,match='crop'):rev.prepare_handoff(root,DAY,c['revision_id'])
    assert not (folder/'handoff.json').exists()


def test_sealed_evidence_recheck_ignores_elapsed_delivery_deadline(display,monkeypatch):
    root,c,folder,*_=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    answer(display,caught.value.job_id)
    ready=resume(display)
    monkeypatch.setattr(rev,'_now',lambda:rev._stamp(c['policy']['deadline'])+timedelta(days=1))
    rev._validate_presentation_final(root,DAY,c['revision_id'],ready['approval'],check_budget=False)
    with pytest.raises(ValueError,match='deadline'):
        rev._validate_presentation_final(root,DAY,c['revision_id'],ready['approval'])


def test_missing_checkpoint_does_not_reenter_science(display,monkeypatch):
    root,c,folder,*_=display
    (folder/'presentation/checkpoint.json').unlink()
    monkeypatch.setattr('daily_agent.pipeline._process_incremental_batch',lambda *a,**k:pytest.fail('No science admission'))
    with pytest.raises(StateCorrupt,match='evidence'):resume(display)
    assert not (folder/'ready.json').exists()


def test_terminal_block_repeated_calls_cost_no_resume_failure_or_model(display,monkeypatch):
    root,c,folder,_,selection=display
    with pytest.raises(queue.PendingResponse) as caught:resume(display)
    bad=deepcopy(selection);bad['assets'][0]['bbox']=[0,0,999,999]
    answer(display,caught.value.job_id,bad)
    blocked=resume(display)
    assert blocked['state']=='presentation_blocked' and blocked['auto_resume'] is False
    assert blocked['operations'][0]['job_id']==caught.value.job_id
    before=(folder/'budget.json').read_bytes()
    marker=(folder/'presentation/blocked.json').read_bytes()
    monkeypatch.setattr('daily_agent.workflow_runtime.run_process',lambda *a,**kw:pytest.fail('No more subprocesses'))
    for _ in range(3):
        assert rev.run_generation(root,DAY,c['revision_id'])==blocked
        assert rev.presentation_status(root,DAY,c['revision_id'])==blocked
        assert rev.freeze_completed(root,DAY,c['revision_id'])==blocked
    assert before==(folder/'budget.json').read_bytes()
    assert marker==(folder/'presentation/blocked.json').read_bytes()
    assert not (folder/'ready.json').exists()
    (folder/'presentation/blocked.json').unlink()
    with pytest.raises(StateCorrupt,match='terminal'):rev.run_generation(root,DAY,c['revision_id'])
    assert before==(folder/'budget.json').read_bytes()
