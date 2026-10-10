"""Offline real queue wave admission; no production data or models."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import time

import pytest

from daily_agent import parent_writer as queue, reading_batches as batches
from daily_agent.batch_dispatch import response_payload, validate_response_payload
from daily_agent.batch_execution import candidate
from daily_agent.models import MaterialRecord
from daily_agent.workflow_runtime import WorkflowCancelled
from daily_agent.workflow_state import read_json
from test_reading_batches import fixture, execution, invoke, answer, note


def queued(f):
    return queue.pending(f.root)


def attempt(f, phase='native'):
    with execution(f) as run:
        if phase == 'repaired':
            run.bind_repaired(f.record)
        try:
            batches.read_papers([f.record], f.config, invoke(f), execution=run, phase=phase)
        except queue.PendingResponse as exc:
            return exc, run.snapshot()
        return None, run.snapshot()


def test_wave_counts_existing_pending_and_refills_only_answered_slot(tmp_path):
    f = fixture(tmp_path, chunks=20)
    f.config.sources['reading']['concurrent_reads'] = 3
    first, state = attempt(f)
    ids = [j['job_id'] for j in queued(f)]
    assert len(ids) == 3 and len(state['operations']) == 12
    second, repeated = attempt(f)
    assert [j['job_id'] for j in queued(f)] == ids
    assert repeated['operations'] == state['operations']
    answer(f, ids[0])
    third, more = attempt(f)
    assert len(queued(f)) == 3
    assert len(more['operations']) == 16
    assert set(ids[1:]).issubset({j['job_id'] for j in queued(f)})
    assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 4


def test_repair_uses_same_wave_capacity(tmp_path):
    f = fixture(tmp_path, chunks=12)
    f.config.sources['reading']['concurrent_reads'] = 2
    attempt(f)
    jobs = queued(f)
    answer(f, jobs[0]['job_id'], {'invalid': True})
    _, state = attempt(f)
    assert len(queued(f)) == 2
    assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 3
    assert any(op['identity'][3] == 1 for op in state['operations'].values())
    # The third original group cannot crowd out the repair's original ordinal.
    assert not any(op['identity'][2] == 'chunk:c0012' for op in state['operations'].values())


def test_queue_capacity_before_admission_is_typed_wait_with_no_false_job(tmp_path, monkeypatch):
    f = fixture(tmp_path, chunks=8)
    f.config.sources['reading']['concurrent_reads'] = 3
    monkeypatch.setattr(queue, 'pending', lambda *a, **kw: [{}]*200)
    exc, state = attempt(f)
    assert isinstance(exc, queue.QueueCapacityPending)
    assert exc.job_id is None and not state['operations']
    payload = response_payload(exc)
    assert payload == {'state':'awaiting_parent_writer','job_id':None,'reason':'queue_capacity','queue_limit':200}
    assert validate_response_payload(payload, '2026-10-09')['reason'] == 'queue_capacity'
    assert not list((f.root/'data/writer-queue').glob('*.job.json'))
    assert validate_response_payload({**payload, 'queue_limit':201}, '2026-10-09') is None


def test_cancel_and_expired_stop_further_admission(tmp_path):
    for exception in (KeyboardInterrupt, WorkflowCancelled, queue.ExpiredResponse):
        f = fixture(tmp_path / exception.__name__, chunks=20)
        f.config.sources['reading']['concurrent_reads'] = 3
        calls = []
        def stopped(*args, **kwargs):
            calls.append(kwargs)
            if exception is queue.ExpiredResponse: raise exception('a'*64)
            raise exception()
        with execution(f) as run, pytest.raises(exception):
            batches.read_papers([f.record], f.config, stopped, execution=run)
        assert len(calls) == 1


@pytest.mark.parametrize('width,expected', [(2,[8,12,15]),(3,[6,8,10])])
def test_synthetic_documents_wave_rounds_and_exact_replay(tmp_path, monkeypatch, width, expected):
    source = json.loads(gzip.decompress((Path(__file__).parent/'fixtures/reading_actual_documents_20261009.json.gz').read_bytes()))
    measured = []
    spans = {}
    for name in ('_manifest', '_validate_receipts', '_pending_jobs', '_cache_note', '_project'):
        original = getattr(batches, name)
        def timed(*args, _name=name, _original=original, **kwargs):
            started=time.perf_counter()
            try:return _original(*args, **kwargs)
            finally:
                bucket=spans.setdefault(_name, {'calls':0,'seconds':0.})
                bucket['calls']+=1;bucket['seconds']+=time.perf_counter()-started
        monkeypatch.setattr(batches,name,timed)
    for index, value in enumerate(source['documents']):
        f = fixture(tmp_path/str(index))
        f.record = MaterialRecord.from_dict(value['record'])
        f.candidates = [candidate(f.record)]
        f.record.paper_document = deepcopy(value['document'])
        f.config.sources['reading']['concurrent_reads'] = width
        durations=[]; rounds=0;spans.clear()
        while True:
            start=time.perf_counter(); exc,_=attempt(f,'repaired');durations.append(time.perf_counter()-start)
            if exc is None: break
            jobs=queued(f)
            assert 1 <= len(jobs) <= width
            for j in jobs: answer(f,j['job_id'])
            rounds+=1
            assert rounds <= value['expected_groups']
        assert rounds == expected[index]
        assert f.record.reading['complete']
        assert len(f.record.reading['notes']) == len(value['document']['chunks'])
        paths=list((f.root/'data/writer-queue').glob('*.job.json'))
        assert len(paths) == value['expected_groups']
        exc,_=attempt(f,'repaired');assert exc is None
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == len(paths)
        measured.append({'groups':len(paths),'wait_rounds':rounds,'resume_seconds':durations,'inclusive_function_spans':deepcopy(spans)})
    print('OFFLINE_WAVE_PROFILE='+json.dumps({'width':width,'documents':measured}))


def test_real_full_queue_has_no_partial_admission_and_recovers(tmp_path):
    f=fixture(tmp_path,chunks=12)
    f.config.sources['reading']['concurrent_reads']=3
    for index in range(200):
        with pytest.raises(queue.PendingResponse):
            queue.request(f.root,f'unrelated exact job {index}',1,stage='reading')
    exc,state=attempt(f)
    assert isinstance(exc,queue.QueueCapacityPending)
    assert not state['operations'] and len(queued(f))==200
    # Answer an unrelated job through the ordinary API; never mutate capacity.
    unrelated=queued(f)[0]['job_id']
    queue.import_response(f.root,unrelated,{},'capacity-fixture')
    exc,state=attempt(f)
    assert isinstance(exc,queue.QueueCapacityPending)
    assert len(state['operations'])==4 and len(queued(f))==200
    before=deepcopy(state['operations'])
    exc,state=attempt(f)
    assert state['operations']==before and len(queued(f))==200


def test_exact_note_projection_does_not_rewrite_but_revalidates_on_resume(tmp_path,monkeypatch):
    f=fixture(tmp_path,chunks=4)
    attempt(f)
    answer(f,queued(f)[0]['job_id'])
    exc,_=attempt(f);assert exc is None
    writes=[];validations=[]
    original_write=batches.atomic_json;original_validate=batches.reading.valid_note
    def write(path,value):
        writes.append(Path(path).name);return original_write(path,value)
    def validate(*a,**kw):
        validations.append(1);return original_validate(*a,**kw)
    monkeypatch.setattr(batches,'atomic_json',write)
    monkeypatch.setattr(batches.reading,'valid_note',validate)
    exc,_=attempt(f);assert exc is None
    assert validations
    assert not any(name.startswith('c000') for name in writes)


def test_exhausted_stage_preserves_all_old_pending_without_new_admission(tmp_path):
    f=fixture(tmp_path,chunks=20)
    f.config.sources['reading']['concurrent_reads']=3
    _,initial=attempt(f)
    ids={j['job_id'] for j in queued(f)}
    with execution(f) as run:
        with run.stage('native'):
            f.clock.now += run.remaining('native')
        assert run.remaining('native')==0
        with pytest.raises(queue.PendingResponse):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert run.snapshot()['operations']==initial['operations']
        assert {j['job_id'] for j in queued(f)}==ids


def test_zero_budget_pending_and_answered_observation_busy_remains_suspended(tmp_path):
    from daily_agent.batch_dispatch import QueueObservationBusy
    for answered in (False,True):
        f=fixture(tmp_path/str(answered),chunks=8)
        f.config.sources['reading']['concurrent_reads']=2
        attempt(f)
        if answered:
            for row in queued(f):answer(f,row['job_id'])
            exc,_=attempt(f);assert exc is None
        with execution(f) as run:
            with run.stage('native'):f.clock.now+=run.remaining('native')
            before=run.snapshot()['operations']
            with queue._QUEUE_THREADS:
                with pytest.raises(QueueObservationBusy):
                    batches.read_papers([f.record],f.config,invoke(f),execution=run)
            assert run.snapshot()['operations']==before


@pytest.mark.parametrize('configured,width',[(0,1),(-1,1),(100,4),(3,3)])
def test_existing_concurrency_setting_retains_original_one_to_four_cap(tmp_path,configured,width):
    f=fixture(tmp_path,chunks=20)
    f.config.sources['reading']['concurrent_reads']=configured
    attempt(f)
    assert len(queued(f))==width
