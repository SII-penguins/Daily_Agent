from copy import deepcopy
import pytest
from daily_agent import batch_execution, parent_writer
from daily_agent.batch_execution import BudgetExhausted, ExecutionConflict
from daily_agent.workflow_state import StateCorrupt, read_json
from test_batch_transport import fixture, execution, operation, job_path


def members(f):return [operation(f),operation(f,substep='chunk:c2')]


def test_two_reading_members_one_real_job_and_two_original_slots(fixture):
    f=fixture
    with execution(f) as ex:
        with ex.stage('native'):
            with pytest.raises(parent_writer.PendingResponse) as raised:
                parent_writer.request(f.root,'Read two exact chunks',20,execution=ex,operations=members(f))
            job=raised.value.job_id;ops=ex.snapshot()['operations']
            assert len(ops)==2 and {op['queue_job_id'] for op in ops.values()}=={job}
            assert len(list(job_path(f,job).parent.glob('*.job.json')))==1
            snapshot=deepcopy(ops)
            with pytest.raises(parent_writer.PendingResponse) as again:
                parent_writer.request(f.root,'Read two exact chunks',20,execution=ex,operations=members(f))
            assert again.value.job_id==job and ex.snapshot()['operations']==snapshot
        assert ex.snapshot()['pools']['native']['limit']==30
    with execution(f) as ex:
        with ex.stage('native'):
            with pytest.raises(parent_writer.PendingResponse) as again:
                parent_writer.request(f.root,'Read two exact chunks',20,execution=ex,operations=members(f))
            assert again.value.job_id==job
            item=ex.existing_operation(members(f)[0]);item['queue_job_id']='tampered'
            assert ex.existing_operation(members(f)[0])['queue_job_id']==job


@pytest.mark.parametrize('bad', ['overflow','duplicate','cross_material','cross_phase','mixed_existing','old_job'])
def test_any_member_failure_admits_nothing_and_creates_no_new_job(fixture,bad):
    f=fixture
    with execution(f) as ex:
        with ex.stage('native'):
            group=members(f)
            if bad in {'mixed_existing','old_job'}:
                with pytest.raises(parent_writer.PendingResponse):
                    parent_writer.request(f.root,'Old exact single input',20,execution=ex,operation=group[0])
            before=ex.snapshot()['operations'];files=list((f.root/'data/writer-queue').glob('*.job.json'))
            if bad=='overflow':group[1]=operation(f,substep='chunk:c3')
            if bad=='duplicate':group[1]=group[0]
            if bad=='cross_material':group[1]=operation(f,index=1)
            if bad=='cross_phase':group[1]=operation(f,phase='visual',substep='page:2')
            with pytest.raises((ExecutionConflict,BudgetExhausted)):
                parent_writer.request(f.root,'New grouped input',20,execution=ex,operations=group)
            assert ex.snapshot()['operations']==before
            assert list((f.root/'data/writer-queue').glob('*.job.json'))==files


def test_existing_group_cannot_gain_new_member_even_with_same_exact_job(fixture):
    f=fixture;f.config.sources['reading']['max_chunks_per_paper']=3
    with execution(f) as ex:
        with ex.stage('native'):
            ex.admit_many(members(f),{'exact':'group'},queue_job_id='real-fixture-job',queue_role='reading')
            before=ex.snapshot()['operations']
            with pytest.raises(ExecutionConflict,match='new members'):
                ex.admit_many(members(f)+[operation(f,substep='chunk:c3')],{'exact':'group'},queue_job_id='real-fixture-job',queue_role='reading')
            assert ex.snapshot()['operations']==before


def test_multislot_write_is_once_and_uncertain_failure_poisoned(fixture,monkeypatch):
    f=fixture
    with execution(f) as ex:
        writes=[];real=batch_execution.atomic_json
        def save(path,value):
            if path==ex.path:writes.append(deepcopy(value))
            return real(path,value)
        monkeypatch.setattr(batch_execution,'atomic_json',save)
        with ex.stage('native'):
            writes.clear()
            ex.admit_many(members(f),{},queue_job_id='one-job',queue_role='reading')
            assert len(writes)==1 and len(writes[0]['payload']['operations'])==2
    with execution(f) as ex:
        with pytest.raises(StateCorrupt):
            with ex.stage('native'):
                def fail(path,value):raise OSError('uncertain write')
                monkeypatch.setattr(batch_execution,'atomic_json',fail)
                with pytest.raises(OSError):
                    ex.admit_many(members(f),{},queue_job_id='one-job',queue_role='reading')
                ex.snapshot()


@pytest.mark.parametrize('variant',['disjoint','single_extension','subset','reverse'])
def test_bound_group_has_exact_immutable_membership_and_order(fixture,variant):
    f=fixture;f.config.sources['reading']['max_chunks_per_paper']=4
    f.records[0].paper_document['chunks'].append({'id':'c4','text':'Fourth exact source'})
    f.candidates=[batch_execution.candidate(r) for r in f.records]
    with execution(f) as ex:
        with ex.stage('native'):
            group=members(f);ex.admit_many(group,{},queue_job_id='one-job',queue_role='reading')
            before=ex.path.read_bytes()
            variants={'disjoint':[operation(f,substep='chunk:c3'),operation(f,substep='chunk:c4')],
                      'single_extension':[operation(f,substep='chunk:c3')], 'subset':[group[0]], 'reverse':group[::-1]}
            with pytest.raises(ExecutionConflict):
                ex.admit_many(variants[variant],{},queue_job_id='one-job',queue_role='reading')
            assert ex.path.read_bytes()==before


@pytest.mark.parametrize('hours',[1,6,8])
def test_optional_deadline_replay_uses_original_creation_time_not_new_now(fixture,hours,monkeypatch):
    from datetime import datetime,timedelta,timezone
    f=fixture;deadline=datetime.now(timezone.utc)+timedelta(hours=hours)
    with execution(f) as ex:
        ex.transport_deadline=lambda:deadline
        with ex.stage('native'):
            with pytest.raises(parent_writer.PendingResponse) as first:
                parent_writer.request(f.root,'Deadline-bound reading',20,execution=ex,operations=members(f))
            job=read_json(job_path(f,first.value.job_id))
            expected=min(datetime.fromisoformat(job['created_at'])+timedelta(hours=6),deadline)
            assert datetime.fromisoformat(job['expires_at'])==expected
            original_datetime=parent_writer.datetime
            class Later(datetime):
                @classmethod
                def now(cls,tz=None):return original_datetime.now(tz)+timedelta(seconds=2)
            monkeypatch.setattr(parent_writer,'datetime',Later)
            # Make the deadline instance match the patched datetime's isinstance check.
            shifted_type_deadline=Later.fromisoformat(deadline.isoformat())
            ex.transport_deadline=lambda:shifted_type_deadline
            with pytest.raises(parent_writer.PendingResponse) as replay:
                parent_writer.request(f.root,'Deadline-bound reading',20,execution=ex,operations=members(f))
            assert replay.value.job_id==first.value.job_id
            assert read_json(job_path(f,first.value.job_id))['expires_at']==job['expires_at']


@pytest.mark.parametrize('phase,substep',[('primary_writer','semantic'),('fidelity','review:2')])
def test_nonreading_single_slots_keep_original_same_job_reuse(fixture,phase,substep):
    f=fixture
    with execution(f) as ex:
        with ex.stage(phase):
            first=ex.admit(*operation(f,phase=phase,substep=substep,ordinal=0),{},queue_job_id='unchanged-answer',queue_role='review')
            second=ex.admit(*operation(f,phase=phase,substep=substep,ordinal=1),{},queue_job_id='unchanged-answer',queue_role='review')
            assert first!=second and len(ex.snapshot()['operations'])==2
