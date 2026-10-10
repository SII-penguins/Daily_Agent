"""Single-supervisor breadth-first suspension; real tmp queue, no model/network."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import pytest

from daily_agent import pipeline, parent_writer, batch_dispatch
from daily_agent.batch_execution import ExecutionConflict
from daily_agent.workflow_state import read_json, atomic_json, StateCorrupt, WorkflowBusy
from test_incremental_pipeline import setup, run, answer, record, qualify, install_writer, STEPS


def waiting(s):
    return read_json(next((s.plan.folder/'batch-execution').glob('*/waiting.json')))['payload']


def test_two_materials_progress_each_round_with_identical_finite_job_count(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch);rounds=0;ids=set()
    for _ in range(len(STEPS)+1):
        try:
            records,_,_=run(s);break
        except batch_dispatch.PendingBatchResponse as pending:
            rounds+=1
            assert [j['material'] for j in pending.jobs]==[r.key for r in s.batch]
            assert len(pending.jobs)==2
            assert waiting(s)['jobs']==pending.jobs
            for row in pending.jobs:
                assert row['job_id'] not in ids
                ids.add(row['job_id']);answer(s,row['job_id'])
    else: pytest.fail('bounded stage rounds did not finish')
    assert rounds==len(STEPS)  # serial baseline needs 2 * len(STEPS) answer/resume rounds
    assert len(ids)==2*len(STEPS)
    assert [r.key for r in records]==[r.key for r in s.batch]
    assert waiting(s)['jobs']==[]
    assert set(waiting(s)['committed_materials'])=={r.key for r in s.batch}
    old=len(s.calls);run(s);assert len(s.calls)==old


def test_eight_frozen_candidates_each_enqueue_once_in_original_order(setup,monkeypatch):
    s=setup;s.batch=[record(f'2609.{n:05d}') for n in range(1,9)];install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised: run(s)
    jobs=raised.value.jobs
    assert [j['material'] for j in jobs]==[r.key for r in s.batch]
    assert len(jobs)==8
    first=deepcopy(jobs)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as again:run(s)
    assert again.value.jobs==first
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==8


def test_b_commit_survives_a_pending_then_a_resume_does_not_model_read_b(setup,monkeypatch):
    s=setup;seen=[]
    def draft(cfg,rows,*,use_llm,execution):
        r=rows[0];seen.append(r.key)
        if r.key==s.batch[0].key:
            with execution.stage('native'):
                parent_writer.request(cfg.root,'A exact input',10,stage='reading',execution=execution,
                                      operation=(r.key,'native','chunk:c1',0))
        return [qualify(r)]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    assert s.batch[1].key in waiting(s)['committed_materials']
    assert s.projections['editorial']['approved']==[s.batch[1].key]
    answer(s,raised.value.job_id);seen.clear();records,_,_=run(s)
    assert seen==[s.batch[0].key]
    assert [r.key for r in records]==[r.key for r in s.batch]


def test_all_jobs_from_threaded_stage_are_observed_not_only_first_exception(setup,monkeypatch):
    s=setup
    for r in s.batch:
        r.paper_document['chunks']=[{'id':f'c{i}','text':'Exact fixture source'} for i in range(1,4)]
    def draft(cfg,rows,*,use_llm,execution):
        r=rows[0];pending=[]
        with execution.stage('native'):
            for chunk in r.paper_document['chunks']:
                try:parent_writer.request(cfg.root,json.dumps([r.key,chunk['id']]),10,stage='reading',
                    execution=execution,operation=(r.key,'native','chunk:'+chunk['id'],0))
                except parent_writer.PendingResponse as exc:pending.append(exc)
        raise pending[0]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    assert len(raised.value.jobs)==6
    assert len({r['job_id'] for r in raised.value.jobs})==6
    assert all(r['role']=='reading' for r in raised.value.jobs)


def test_expiry_is_explicit_and_never_creates_replacement_slot(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    first=raised.value.jobs
    path=s.config.root/'data/writer-queue'/f"{first[0]['job_id']}.job.json"
    job=read_json(path);job['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();atomic_json(path,job)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as again:run(s)
    assert again.value.expired
    assert [j['status'] for j in again.value.jobs]==['expired','pending']
    assert [j['job_id'] for j in again.value.jobs]==[j['job_id'] for j in first]
    assert len(list(path.parent.glob('*.job.json')))==2


def test_claims_preserve_ownership_and_inactive_lease_is_observational(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    job_id=raised.value.job_id;lease=parent_writer.claim(s.config.root,job_id,'worker-a')
    with pytest.raises(WorkflowBusy):parent_writer.claim(s.config.root,job_id,'worker-b')
    with pytest.raises(batch_dispatch.PendingBatchResponse) as claimed:run(s)
    assert claimed.value.jobs[0]['claim']['active'] is True
    assert 'token' not in claimed.value.jobs[0]['claim']
    path=s.config.root/'data/writer-queue'/f'{job_id}.claim.json'
    lease['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();atomic_json(path,lease)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as inactive:run(s)
    assert inactive.value.jobs[0]['claim']['active'] is False
    with pytest.raises(ValueError):parent_writer.import_response(s.config.root,job_id,{},'worker-a',claim_token=lease['token'])
    assert read_json(path)==lease  # observation never steals or resets a claim


@pytest.mark.parametrize('failure',[KeyboardInterrupt,RuntimeError,StateCorrupt])
def test_cancellation_or_failure_does_not_dispatch_later_siblings(setup,monkeypatch,failure):
    s=setup;seen=[]
    def draft(cfg,rows,**kwargs):seen.append(rows[0].key);raise failure('stop')
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(failure):run(s)
    assert seen==[s.batch[0].key]


def test_unbound_pending_id_is_not_a_dispatch_capability(setup,monkeypatch):
    s=setup;seen=[]
    def draft(cfg,rows,**kwargs):seen.append(rows[0].key);raise parent_writer.PendingResponse('f'*64)
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(ExecutionConflict,match='not bound'):run(s)
    assert seen==[s.batch[0].key]


def test_role_separation_still_enforced_for_sibling_jobs(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as first:run(s)
    for row in first.value.jobs:answer(s,row['job_id'])
    with pytest.raises(batch_dispatch.PendingBatchResponse) as second:run(s)
    assert all(row['role']=='review' for row in second.value.jobs)
    job_id=second.value.job_id;lease=parent_writer.claim(s.config.root,job_id,'offline-test-writer')
    with pytest.raises(ValueError,match='different worker'):
        parent_writer.import_response(s.config.root,job_id,{},'offline-test-writer',claim_token=lease['token'])


def test_wait_set_cannot_restore_publication_eligibility(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse):run(s)
    # Caller has re-evaluated eligibility and excluded both frozen candidates.
    before=len(s.calls)
    with pytest.raises(batch_dispatch.PendingBatchResponse):
        pipeline._process_incremental_batch(s.config,__import__('datetime').date(2026,10,10),deepcopy(s.batch),
            s.plan,'original-batch',s.library,[],[],[],use_llm=True,eligible_keys=set())
    assert len(s.calls)==before and len(waiting(s)['jobs'])==2
    assert waiting(s)['committed_materials']==[]


def test_identical_actual_job_is_deduplicated_with_all_material_bindings(setup,monkeypatch):
    s=setup
    def draft(cfg,rows,*,use_llm,execution):
        with execution.stage('native'):
            parent_writer.request(cfg.root,'Identical shared source chunk',10,stage='reading',execution=execution,
                                  operation=(rows[0].key,'native','chunk:c1',0))
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    assert len(raised.value.jobs)==1
    assert [b['material'] for b in raised.value.jobs[0]['bindings']]==[r.key for r in s.batch]
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==1


def test_wait_set_write_failure_preserves_exact_jobs_on_resume(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch);original=batch_dispatch.atomic_json
    def fail(*a,**k):raise OSError('fsync failure')
    monkeypatch.setattr(batch_dispatch,'atomic_json',fail)
    with pytest.raises(OSError):run(s)
    first={p.name for p in (s.config.root/'data/writer-queue').glob('*.job.json')}
    assert len(first)==1
    monkeypatch.setattr(batch_dispatch,'atomic_json',original)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    assert len(raised.value.jobs)==2
    assert first <= {p.name for p in (s.config.root/'data/writer-queue').glob('*.job.json')}


def test_partial_projection_crash_does_not_lose_committed_sibling(setup,monkeypatch):
    s=setup;seen=[]
    def draft(cfg,rows,*,use_llm,execution):
        r=rows[0];seen.append(r.key)
        if r.key==s.batch[0].key:
            with execution.stage('native'):
                parent_writer.request(cfg.root,'A fixed wait',10,stage='reading',execution=execution,
                                      operation=(r.key,'native','chunk:c1',0))
        return [qualify(r)]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    save=pipeline.write_editorial_artifacts
    def fail(*a,**k):raise OSError('projection crash')
    monkeypatch.setattr(pipeline,'write_editorial_artifacts',fail)
    with pytest.raises(OSError):run(s)
    state=read_json(next((s.plan.folder/'batch-execution').glob('*/journal.json')))['payload']
    assert s.batch[1].key in state['completed']
    monkeypatch.setattr(pipeline,'write_editorial_artifacts',save);seen.clear()
    with pytest.raises(batch_dispatch.PendingBatchResponse):run(s)
    assert seen==[s.batch[0].key]
    assert s.projections['editorial']['approved']==[s.batch[1].key]


def test_second_dispatcher_cannot_enter_same_execution_lease(setup,monkeypatch):
    import threading
    s=setup;entered=threading.Event();release=threading.Event();errors=[]
    def draft(cfg,rows,**kwargs):
        entered.set()
        assert release.wait(5)
        return [qualify(rows[0])]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    def outer():
        try:run(s)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=outer);thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(WorkflowBusy):run(s)
    finally:release.set();thread.join(5)
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize('close_pool',['circuit','budget'])
def test_previously_admitted_job_remains_gate_after_later_sibling_closes_pool(setup,monkeypatch,close_pool):
    from daily_agent.models import EditorialDraft
    s=setup;phase='primary_writer' if close_pool=='circuit' else 'native'
    substep='draft' if close_pool=='circuit' else 'chunk:c1'
    def draft(cfg,rows,*,use_llm,execution):
        r=rows[0];state=execution.snapshot()
        available=state['writer_circuit'] is None if close_pool=='circuit' else state['pools'][phase]['remaining']>0
        if r.key==s.batch[0].key and available:
            with execution.stage(phase):
                parent_writer.request(cfg.root,'Already admitted fixed input',10,stage='draft',execution=execution,
                                      operation=(r.key,phase,substep,0))
        if r.key==s.batch[1].key and available:
            if close_pool=='circuit':execution.writer_failure('backend','fixture failure')
            else:
                with execution.stage(phase):s.clock.now+=1800
        return [EditorialDraft(r.key,'paper',r.title,{})]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as first:run(s)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as second:run(s)
    assert second.value.job_id==first.value.job_id
    assert len(waiting(s)['jobs'])==1 and not waiting(s)['committed_materials']
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==1


def test_queue_observation_contention_suspends_instead_of_failed_generation(setup,monkeypatch):
    from contextlib import contextmanager
    s=setup;install_writer(s,monkeypatch);original=parent_writer.queue_lock
    @contextmanager
    def contend(path,*,timeout=None,strict_io=False):
        if timeout==1:raise TimeoutError('another worker is importing')
        with original(path,timeout=timeout,strict_io=strict_io):yield
    monkeypatch.setattr(parent_writer,'queue_lock',contend)
    with pytest.raises(batch_dispatch.QueueObservationBusy) as raised:run(s)
    payload=batch_dispatch.response_payload(raised.value)
    assert payload['reason']=='queue_observation_busy' and 'jobs' not in payload
    assert read_json(s.config.root/'data/writer-queue'/f'{raised.value.job_id}.job.json')
    monkeypatch.setattr(parent_writer,'queue_lock',original)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as resumed:run(s)
    assert len(resumed.value.jobs)==2


def test_mixed_expiry_legacy_id_points_to_expired_job_without_reordering(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as first:run(s)
    expired_id=first.value.jobs[1]['job_id'];path=s.config.root/'data/writer-queue'/f'{expired_id}.job.json'
    job=read_json(path);job['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();atomic_json(path,job)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as second:run(s)
    assert second.value.job_id==expired_id
    assert [r['status'] for r in second.value.jobs]==['pending','expired']
    assert batch_dispatch.response_payload(second.value)['state']=='expired_parent_writer'


def test_full_queue_keeps_existing_batch_wait_set_and_has_no_false_admission(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    original=parent_writer.pending
    calls=[]
    def capacity(*a,**k):
        calls.append(1)
        return [] if len(calls)==1 else [{}]*200
    monkeypatch.setattr(parent_writer,'pending',capacity)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as raised:run(s)
    assert len(raised.value.jobs)==1
    assert raised.value.jobs[0]['material']==s.batch[0].key
    payload=batch_dispatch.response_payload(raised.value)
    assert payload['reason']=='queue_capacity'
    assert batch_dispatch.validate_response_payload(payload,'2026-10-10')['jobs']==raised.value.jobs
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==1


def test_shared_job_file_read_once_each_observation_but_each_binding_verified(setup,monkeypatch):
    s=setup
    def draft(cfg,rows,*,use_llm,execution):
        with execution.stage('native'):
            parent_writer.request(cfg.root,'Shared source input',10,stage='reading',execution=execution,
                                  operation=(rows[0].key,'native','chunk:c1',0))
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse):run(s)
    reads=[];original=batch_dispatch.read_json
    def read(path):
        if str(path).endswith('.job.json'):reads.append(str(path))
        return original(path)
    monkeypatch.setattr(batch_dispatch,'read_json',read)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as again:run(s)
    # Three separate locked observations (after A, after B, final) each read once.
    assert len(reads)==3 and len(set(reads))==1
    assert len(again.value.jobs[0]['bindings'])==2


def test_reading_wave_three_leaves_three_slots_for_next_frozen_paper(setup,monkeypatch):
    from daily_agent import reading_batches
    from daily_agent.paper_document import build_document,attach_document
    s=setup;s.config.sources['reading']['concurrent_reads']=3
    labels=['1. Introduction','2. Methods','3. Results','4. References','5. Appendix']
    text=''.join(labels[i%5]+f'\nExact source example {i}: '+'bounded scientific evidence. '*8+'\n' for i in range(20))
    for material in s.batch:
        attach_document(material,build_document(material,[{'page':1,'text':text}],material.url,'html',{'min_body_chars':10}))
        assert len(material.paper_document['chunks'])==20
    def draft(cfg,rows,*,use_llm,execution):
        def invoke(prompt,timeout,**kwargs):return parent_writer.request(cfg.root,prompt,timeout,**kwargs)
        reading_batches.read_papers(rows,cfg,invoke,execution=execution)
        pytest.fail('Pending full reading cannot prematurely draft')
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    with pytest.raises(batch_dispatch.PendingBatchResponse) as first:run(s)
    assert [j['material'] for j in first.value.jobs]==[s.batch[0].key]*3+[s.batch[1].key]*3
    ids=[j['job_id'] for j in first.value.jobs]
    with pytest.raises(batch_dispatch.PendingBatchResponse) as second:run(s)
    assert [j['job_id'] for j in second.value.jobs]==ids
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==6
