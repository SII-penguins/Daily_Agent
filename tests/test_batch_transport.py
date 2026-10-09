"""Offline explicit queue admission, derivative topology, and frozen author caps."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from daily_agent import batch_execution, parent_writer
from daily_agent.batch_execution import BudgetExhausted, Execution, ExecutionConflict, candidate, digest
from daily_agent.models import MaterialRecord
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fixture(tmp_path):
    records = []
    for index in range(4):
        record = MaterialRecord(key=f'arxiv:2609.0000{index}', source='arxiv', item_type='paper',
                                title=f'Offline paper {index}', url=f'https://example.test/{index}')
        record.paper_document = {
            'chunks': [{'id': f'c{c}', 'text': f'Native source {c}'} for c in range(1, 4)],
            'pages': [{'page': 1}, {'page': 2, 'visual_required': True}],
        }
        records.append(record)
    config = SimpleNamespace(root=tmp_path, sources={
        'reading': {'run_budget_seconds': 30, 'visual_budget_seconds': 20,
                    'fidelity_budget_seconds': 50, 'max_chunks_per_paper': 2},
        'llm_writer': {'run_budget_seconds': 60, 'provider': 'parent_queue'},
        'author_context': {'max_research_papers_per_batch': 2},
    })
    return SimpleNamespace(root=tmp_path, config=config, clock=Clock(), records=records,
                           candidates=[candidate(record) for record in records])


def execution(fixture):
    return Execution(fixture.root / 'issue', 'frozen-batch', fixture.candidates, fixture.config,
                     protocol='offline-transport-v1', clock=fixture.clock)


def operation(fixture, phase='native', substep='chunk:c1', ordinal=0, index=0):
    return (fixture.records[index].key, phase, substep, ordinal)


def queue_request(fixture, run, prompt='Frozen prompt', *, op=None, **kwargs):
    with pytest.raises(parent_writer.PendingResponse) as caught:
        parent_writer.request(fixture.root, prompt, 100, execution=run,
                              operation=op or operation(fixture), **kwargs)
    return caught.value.job_id


def job_path(fixture, job_id):
    return fixture.root / 'data' / 'writer-queue' / f'{job_id}.job.json'


def repaired(fixture, *, index=0):
    record = MaterialRecord.from_dict(fixture.records[index].to_dict())
    native = deepcopy(record.paper_document)
    record.paper_document = {
        'evidence_basis': 'image_transcription_reviewed', 'native_document': native,
        'chunks': [{'id': f'derived-{c}', 'text': f'Reviewed text {c}'} for c in range(3)],
        'pages': deepcopy(native['pages']),
    }
    return record


def write_sealed(path, payload):
    atomic_json(path, {'sha256': digest(payload), 'payload': payload})


def test_remaining_reports_active_deadline_and_settled_remainder(fixture):
    run = execution(fixture)
    with pytest.raises(ExecutionConflict):
        run.remaining('native')
    with run:
        assert run.remaining('native') == 30
        with run.stage('native'):
            assert run.snapshot()['pools']['native']['remaining'] == 0
            fixture.clock.advance(7)
            assert run.remaining('native') == 23
            with run.stage('native'):
                fixture.clock.advance(2)
                assert run.remaining('native') == 21
        assert run.remaining('native') == 21
        with pytest.raises(ExecutionConflict):
            run.remaining('unknown')
        with run.stage('native'):
            fixture.clock.advance(25)
            assert run.remaining('native') == 0
        assert run.remaining('native') == 0


@pytest.mark.parametrize('bad_clock', [-1, float('nan'), float('inf')])
def test_remaining_rejects_invalid_active_clock(fixture, bad_clock):
    with execution(fixture) as run:
        with run.stage('native'):
            fixture.clock.now = bad_clock
            with pytest.raises(ExecutionConflict):
                run.remaining('native')
            fixture.clock.now = 100


def test_prompt_images_stage_hashed_under_lock_and_admitted_before_job(fixture, monkeypatch):
    image = fixture.root / 'page.png'
    image.write_bytes(b'before lock')
    original_lock = parent_writer.queue_lock
    entered = False

    @contextmanager
    def delayed_lock(path, **kwargs):
        nonlocal entered
        assert kwargs['timeout'] == 30
        with original_lock(path, **kwargs):
            entered = True
            image.write_bytes(b'actual locked image')
            fixture.clock.advance(4)
            yield
            entered = False

    monkeypatch.setattr(parent_writer, 'queue_lock', delayed_lock)
    original_atomic = parent_writer.atomic_json
    with execution(fixture) as run:
        def guarded_write(path, value):
            if path.name.endswith('.job.json'):
                assert entered
                admitted = run.snapshot()['operations'][digest(list(operation(fixture)))]
                assert admitted['queue_job_id'] == value['job_id']
                assert admitted['input_sha256'] == digest(parent_writer._contract(value))
            original_atomic(path, value)
        monkeypatch.setattr(parent_writer, 'atomic_json', guarded_write)
        with run.stage('native'):
            job = queue_request(fixture, run, image_path=str(image), stage='review')
        payload = read_json(job_path(fixture, job))
        assert payload['call_timeout_seconds'] == 26
        assert payload['images'] == [{'path': 'page.png', 'sha256': hashlib.sha256(b'actual locked image').hexdigest()}]
        op = run.snapshot()['operations'][digest(list(operation(fixture)))]
        assert op['queue_role'] == 'review'
        assert op['identity'][1] == 'native'
        assert run.snapshot()['pools']['native']['charged'] == 4
        assert run.snapshot()['writer_circuit'] is None


def test_pending_resumes_and_answer_reuse_keep_one_exact_slot(fixture):
    jobs = set()
    for _ in range(10):
        with execution(fixture) as run:
            with run.stage('native'):
                fixture.clock.advance(.5)
                jobs.add(queue_request(fixture, run, stage='reading'))
            assert len(run.snapshot()['operations']) == 1
        fixture.clock.advance(3600)
    assert len(jobs) == 1
    job = jobs.pop()
    parent_writer.import_response(fixture.root, job, {'source': 'validated later'}, 'offline-reader')
    saved = job_path(fixture, job).read_bytes()
    with execution(fixture) as run:
        with run.stage('native'):
            response = parent_writer.request(fixture.root, 'Frozen prompt', 100, stage='reading',
                                             execution=run, operation=operation(fixture))
        assert response == {'source': 'validated later'}
        assert run.remaining('native') == 25
        assert len(run.snapshot()['operations']) == 1
    assert job_path(fixture, job).read_bytes() == saved
    assert not issubclass(parent_writer.PendingResponse, Exception)


@pytest.mark.parametrize('changed', ['prompt', 'stage', 'image'])
def test_changed_exact_queue_contract_cannot_create_an_extra_job(fixture, changed):
    image = fixture.root / 'page.png'
    image.write_bytes(b'first')
    with execution(fixture) as run:
        with run.stage('native'):
            job = queue_request(fixture, run, stage='reading', image_path=str(image))
            before = run.path.read_bytes()
            if changed == 'image':
                image.write_bytes(b'second')
            with pytest.raises(StateCorrupt):
                parent_writer.request(fixture.root, 'Changed' if changed == 'prompt' else 'Frozen prompt',
                                      30, str(image), stage='review' if changed == 'stage' else 'reading',
                                      execution=run, operation=operation(fixture))
            assert run.path.read_bytes() == before
            assert [p.name for p in job_path(fixture, job).parent.glob('*.job.json')] == [f'{job}.job.json']


def test_deadline_after_waiting_for_lock_creates_no_job_or_admission(fixture, monkeypatch):
    @contextmanager
    def expired_lock(path, **kwargs):
        fixture.clock.advance(kwargs['timeout'])
        yield
    monkeypatch.setattr(parent_writer, 'queue_lock', expired_lock)
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(BudgetExhausted):
                parent_writer.request(fixture.root, 'prompt', 30, execution=run, operation=operation(fixture))
        assert not run.snapshot()['operations']
        assert not list((fixture.root / 'data' / 'writer-queue').glob('*.job.json'))


@pytest.mark.parametrize('kwargs', [
    {'operation': ('key', 'native', 'chunk:c1', 0)},
    {'execution': True}, {'execution': True, 'operation': ('key', 'native')},
    {'execution': True, 'operation': ('key', 'native', 'chunk:c1', True)},
])
def test_partial_or_invalid_execution_binding_is_rejected(fixture, kwargs):
    with execution(fixture) as run:
        if kwargs.get('execution'):
            kwargs = {**kwargs, 'execution': run}
        with pytest.raises(ExecutionConflict):
            parent_writer.request(fixture.root, 'prompt', 30, **kwargs)
        assert not run.snapshot()['operations']


@pytest.mark.parametrize('timeout', [0, -1, True, '30', float('nan'), float('inf')])
def test_explicit_timeout_must_be_finite_positive(fixture, timeout):
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(ExecutionConflict):
                parent_writer.request(fixture.root, 'prompt', timeout, execution=run, operation=operation(fixture))
        assert not run.snapshot()['operations']


def test_execution_transport_requires_active_stage(fixture):
    with execution(fixture) as run:
        with pytest.raises(ExecutionConflict):
            parent_writer.request(fixture.root, 'prompt', 30, execution=run, operation=operation(fixture))
        assert not run.snapshot()['operations']
        assert not list((fixture.root / 'data' / 'writer-queue').glob('*.job.json'))


def test_existing_unanswered_timeout_only_decreases(fixture):
    with pytest.raises(parent_writer.PendingResponse) as caught:
        parent_writer.request(fixture.root, 'Frozen prompt', 300)
    path = job_path(fixture, caught.value.job_id)
    with execution(fixture) as run:
        with run.stage('native'):
            fixture.clock.advance(5)
            assert queue_request(fixture, run) == caught.value.job_id
            assert read_json(path)['call_timeout_seconds'] == 25
        with run.stage('native'):
            queue_request(fixture, run)
            assert read_json(path)['call_timeout_seconds'] == 25


def test_expired_response_stays_explicit_and_does_not_open_circuit(fixture):
    with execution(fixture) as run:
        with run.stage('primary_writer'):
            job = queue_request(fixture, run, op=operation(fixture, 'primary_writer', 'draft'))
        path = job_path(fixture, job)
        value = read_json(path)
        value['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        atomic_json(path, value)
        with pytest.raises(parent_writer.ExpiredResponse) as caught:
            with run.stage('primary_writer'):
                fixture.clock.advance(1)
                parent_writer.request(fixture.root, 'Frozen prompt', 30, execution=run,
                                      operation=operation(fixture, 'primary_writer', 'draft'))
        assert caught.value.job_id == job
        assert run.snapshot()['writer_circuit'] is None
        assert run.remaining('primary_writer') == 59
        assert len(run.snapshot()['operations']) == 1


def retry_fixture(fixture, old, monkeypatch):
    from daily_agent import cloud_workflow
    now = datetime.now(timezone.utc)
    day = now.date()
    state_dir = fixture.root / 'state'
    budget_path = state_dir / 'cloud-generation-budgets' / f'{day}.json'
    budget = {'deadline': (now + timedelta(hours=1)).isoformat(), 'runtime_seconds': 0,
              'max_runtime_seconds': 1000, 'failures': 0, 'max_failures': 3,
              'resumes': 0, 'max_resumes': 5}
    atomic_json(budget_path, budget)
    monkeypatch.setattr(cloud_workflow, '_config', lambda root: SimpleNamespace(state_dir=state_dir))
    monkeypatch.setattr(cloud_workflow, '_validate_budget', lambda value, date: None)
    path = job_path(fixture, old)
    job = read_json(path)
    job['expires_at'] = (now - timedelta(seconds=1)).isoformat()
    atomic_json(path, job)
    return parent_writer.retry_expired(fixture.root, old, day, 'Offline explicit retry fixture')


def test_retry_generation_cannot_rebind_existing_slot(fixture, monkeypatch):
    with execution(fixture) as run:
        with run.stage('native'):
            old = queue_request(fixture, run)
        new = retry_fixture(fixture, old, monkeypatch)
        assert new['job_id'] != old
        before = deepcopy(run.snapshot()['operations'])
        with run.stage('native'):
            with pytest.raises(ExecutionConflict):
                queue_request(fixture, run)
        assert run.snapshot()['operations'] == before
        assert len(list(job_path(fixture, old).parent.glob('*.job.json'))) == 2


def test_first_admission_binds_actual_active_retry_id_and_contract(fixture, monkeypatch):
    with pytest.raises(parent_writer.PendingResponse) as caught:
        parent_writer.request(fixture.root, 'Frozen prompt', 30)
    new = retry_fixture(fixture, caught.value.job_id, monkeypatch)
    with execution(fixture) as run:
        with run.stage('native'):
            assert queue_request(fixture, run) == new['job_id']
        op = next(iter(run.snapshot()['operations'].values()))
        assert op['queue_job_id'] == new['job_id']
        assert op['input_sha256'] == digest(parent_writer._contract(new))
        assert op['retry_generation'] is None


def test_repaired_binding_is_finite_immutable_and_preserves_identity_and_budgets(fixture):
    record = repaired(fixture)
    with execution(fixture) as run:
        marker = (run.folder / 'initialized.json').read_bytes()
        contract = deepcopy(run.contract)
        pools = deepcopy(run.snapshot()['pools'])
        binding = run.bind_repaired(record)
        assert binding['chunks'] == ['derived-0', 'derived-1', 'derived-2']
        assert binding['document_sha256'] == digest(record.paper_document)
        bound_bytes = run.path.read_bytes()
        assert run.bind_repaired(record.to_dict()) == binding
        assert run.path.read_bytes() == bound_bytes
        binding['chunks'].append('caller-mutation')
        assert run.repaired_binding(record.key)['chunks'] == ['derived-0', 'derived-1', 'derived-2']
        assert run.contract == contract
        assert run.snapshot()['pools'] == pools
        assert (run.folder / 'initialized.json').read_bytes() == marker
        with run.stage('repaired'):
            for ordinal in range(2):
                queue_request(fixture, run, f'repaired attempt {ordinal}',
                              op=operation(fixture, 'repaired', 'chunk:derived-0', ordinal))
            for substep, ordinal in [('chunk:c1', 0), ('chunk:derived-2', 0), ('chunk:derived-0', 2)]:
                with pytest.raises(BudgetExhausted):
                    queue_request(fixture, run, 'outside frozen cap', op=operation(fixture, 'repaired', substep, ordinal))
        assert len(run.snapshot()['operations']) == 2
    with execution(fixture) as run:
        assert run.bind_repaired(record)['document_sha256'] == digest(record.paper_document)
        assert run.contract == contract
        assert run.snapshot()['pools'] == pools
        assert (run.folder / 'initialized.json').read_bytes() == marker


def test_repaired_transport_requires_bound_reviewed_document(fixture):
    with execution(fixture) as run:
        with run.stage('repaired'):
            with pytest.raises(ExecutionConflict):
                queue_request(fixture, run, op=operation(fixture, 'repaired'))
        assert not run.snapshot()['operations']
        assert run.repaired_binding(fixture.records[0].key) is None


@pytest.mark.parametrize('change', ['unreviewed', 'native', 'duplicate', 'no_chunks', 'title', 'other_key', 'chunk_id'])
def test_invalid_repaired_binding_preserves_journal(fixture, change):
    record = repaired(fixture).to_dict()
    doc = record['paper_document']
    if change == 'unreviewed':
        doc['evidence_basis'] = 'unreviewed'
    elif change == 'native':
        doc['native_document']['chunks'][0]['text'] = 'Changed source'
    elif change == 'duplicate':
        doc['chunks'][1]['id'] = doc['chunks'][0]['id']
    elif change == 'no_chunks':
        doc['chunks'] = []
    elif change == 'title':
        record['title'] = 'Different source'
    elif change == 'other_key':
        record['key'] = 'arxiv:unknown'
    else:
        doc['chunks'][0]['id'] = 123
    with execution(fixture) as run:
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.bind_repaired(record)
        assert run.path.read_bytes() == before


def test_repaired_binding_cannot_change_or_replace_legacy_admission(fixture):
    record = repaired(fixture)
    with execution(fixture) as run:
        run.bind_repaired(record)
        before = run.path.read_bytes()
        record.paper_document['chunks'][0]['text'] = 'Different derivative'
        with pytest.raises(ExecutionConflict):
            run.bind_repaired(record)
        assert run.path.read_bytes() == before
        with run.stage('repaired'):
            run.admit(fixture.records[1].key, 'repaired', 'chunk:c1', 0, {}, queue_job_id='legacy-job')
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.bind_repaired(repaired(fixture, index=1))
        assert run.path.read_bytes() == before


@pytest.mark.parametrize('change', ['hash', 'chunks', 'native', 'unknown_material', 'binding_schema'])
def test_repaired_binding_corruption_fails_on_reopen_without_reset(fixture, change):
    with execution(fixture) as run:
        run.bind_repaired(repaired(fixture))
        payload = run.snapshot()
        path = run.path
    bindings = payload['repaired_bindings']
    binding = bindings[fixture.records[0].key]
    if change == 'hash':
        binding['document_sha256'] = '0' * 64
    elif change == 'chunks':
        binding['chunks'].append('extra')
    elif change == 'native':
        binding['document']['native_document']['chunks'][0]['text'] = 'Tamper'
        binding['document_sha256'] = digest(binding['document'])
    elif change == 'unknown_material':
        bindings['unknown'] = bindings.pop(fixture.records[0].key)
    else:
        binding['unexpected'] = True
    write_sealed(path, payload)
    damaged = path.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(fixture):
            pytest.fail('Invalid derivative topology must fail closed')
    assert path.read_bytes() == damaged


def test_already_reviewed_frozen_document_can_bind_without_reset(fixture):
    record = repaired(fixture)
    fixture.records[0] = record
    fixture.candidates[0] = candidate(record)
    with execution(fixture) as run:
        assert run.bind_repaired(record)['document'] == record.paper_document
        assert run.contract['candidates'][0]['input_sha256'] == digest(record.to_dict())


def test_completion_must_use_exact_bound_derivative(fixture):
    record = repaired(fixture)
    with execution(fixture) as run:
        run.bind_repaired(record)
        result = dict(record=record.to_dict(), draft={}, review={}, science={}, evidence={}, asset_hashes={})
        sha = run.prepare_completion(record.key, **result)
        run.commit_completion(record.key, sha, validator=lambda value: True)
        result['record']['paper_document']['chunks'][0]['text'] = 'Changed reviewed evidence'
        with pytest.raises(ExecutionConflict):
            run.prepare_completion(record.key, **result)
    with execution(fixture) as run:
        assert run.completed(record.key, validator=lambda value: True)['record'] == record.to_dict()


def test_binding_cannot_replace_already_completed_native_evidence(fixture):
    with execution(fixture) as run:
        record = fixture.records[0]
        sha = run.prepare_completion(record.key, record=record.to_dict(), draft={}, review={}, science={},
                                     evidence={}, asset_hashes={})
        run.commit_completion(record.key, sha, validator=lambda value: True)
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.bind_repaired(repaired(fixture))
        assert run.path.read_bytes() == before


def test_author_selection_is_original_ordered_subset_and_persists(fixture):
    chosen = [fixture.records[1].key, fixture.records[3].key]
    with execution(fixture) as run:
        before = deepcopy(run.snapshot()['pools'])
        marker = (run.folder / 'initialized.json').read_bytes()
        assert run.author_candidates() is None
        assert not run.author_allowed(chosen[0])
        assert run.bind_author_candidates(chosen) == chosen
        bound_bytes = run.path.read_bytes()
        assert run.bind_author_candidates(chosen) == chosen
        assert run.path.read_bytes() == bound_bytes
        returned = run.author_candidates()
        returned.clear()
        assert run.author_candidates() == chosen
        with run.stage('primary_writer'):
            queue_request(fixture, run, op=operation(fixture, 'primary_writer', 'author_research', index=1))
            with pytest.raises(ExecutionConflict):
                queue_request(fixture, run, op=operation(fixture, 'primary_writer', 'author_research', index=0))
        assert run.snapshot()['pools'] == before
        assert (run.folder / 'initialized.json').read_bytes() == marker
    with execution(fixture) as run:
        assert run.author_candidates() == chosen
        with pytest.raises(ExecutionConflict):
            run.bind_author_candidates([fixture.records[0].key])
        assert run.author_allowed(chosen[-1])


@pytest.mark.parametrize('keys', [None, 'key', ['unknown'], [True], [0, 0], [2, 1], [0, 1, 2]])
def test_author_binding_rejects_changed_order_unknowns_and_cap_expansion(fixture, keys):
    chosen = [fixture.records[k].key if type(k) is int else k for k in keys] if isinstance(keys, list) else keys
    with execution(fixture) as run:
        before = run.path.read_bytes()
        with pytest.raises(ExecutionConflict):
            run.bind_author_candidates(chosen)
        assert run.path.read_bytes() == before


def test_author_unbound_transport_and_legacy_rebinding_fail_closed(fixture):
    with execution(fixture) as run:
        with run.stage('primary_writer'):
            with pytest.raises(ExecutionConflict):
                queue_request(fixture, run, op=operation(fixture, 'primary_writer', 'author_review'))
            run.admit(*operation(fixture, 'primary_writer', 'author_review'), {}, queue_job_id='legacy')
        with pytest.raises(ExecutionConflict):
            run.bind_author_candidates([fixture.records[0].key])


def test_author_config_change_rejected_and_evidence_lineage_allowed(fixture):
    with execution(fixture) as run:
        record = fixture.records[0].to_dict()
        record['raw']['author_research_evidence'] = {'input': {'key': record['key']}, 'proposal': {}, 'review': {}}
        run.prepare_completion(record['key'], record=record, draft={}, review={}, science={}, evidence={}, asset_hashes={})
        path = run.path
    before = path.read_bytes()
    fixture.config.sources['author_context']['max_research_papers_per_batch'] += 1
    with pytest.raises(ExecutionConflict):
        with execution(fixture):
            pytest.fail('Author cap must not reset across resumes')
    assert path.read_bytes() == before


def test_author_binding_corruption_rejected_on_reopen(fixture):
    with execution(fixture) as run:
        run.bind_author_candidates([fixture.records[1].key])
        payload, path = run.snapshot(), run.path
    payload['author_candidates'] = [record.key for record in fixture.records]
    write_sealed(path, payload)
    before = path.read_bytes()
    with pytest.raises(StateCorrupt):
        with execution(fixture):
            pytest.fail('Recomputed outer checksum cannot enlarge frozen author cap')
    assert path.read_bytes() == before


def test_author_absolute_cap_is_twelve_and_empty_binding_stays_bound(fixture):
    fixture.config.sources['author_context']['max_research_papers_per_batch'] = 1000
    for index in range(4, 14):
        record = MaterialRecord(key=f'arxiv:extra-{index}', source='arxiv', item_type='paper',
                                title=f'Extra {index}', url=f'https://example.test/extra-{index}')
        fixture.candidates.append(candidate(record))
    with execution(fixture) as run:
        with pytest.raises(ExecutionConflict):
            run.bind_author_candidates([c['key'] for c in fixture.candidates[:13]])
        assert run.bind_author_candidates([]) == []
        with pytest.raises(ExecutionConflict):
            run.bind_author_candidates([fixture.candidates[0]['key']])
    with execution(fixture) as run:
        assert run.author_candidates() == []


@pytest.mark.parametrize('kind', ['repaired', 'author'])
@pytest.mark.parametrize('write_reached_disk', [False, True])
def test_uncertain_topology_write_requires_reopen_without_budget_reset(fixture, monkeypatch, kind, write_reached_disk):
    original_atomic = batch_execution.atomic_json
    run = execution(fixture)
    with run:
        with run.stage('native'):
            fixture.clock.advance(3)
        marker = (run.folder / 'initialized.json').read_bytes()
        before = deepcopy(run.snapshot()['pools'])

        def uncertain(path, value):
            if write_reached_disk:
                original_atomic(path, value)
            raise OSError('Uncertain topology write')

        monkeypatch.setattr(batch_execution, 'atomic_json', uncertain)
        with pytest.raises(OSError, match='Uncertain topology write'):
            if kind == 'repaired':
                run.bind_repaired(repaired(fixture))
            else:
                run.bind_author_candidates([fixture.records[1].key])
        with pytest.raises(ExecutionConflict):
            run.remaining('native')
    monkeypatch.setattr(batch_execution, 'atomic_json', original_atomic)
    with run:
        assert run.snapshot()['pools'] == before
        assert (run.folder / 'initialized.json').read_bytes() == marker
        saved = run.repaired_binding(fixture.records[0].key) if kind == 'repaired' else run.author_candidates()
        assert (saved is not None) == write_reached_disk
        if kind == 'repaired':
            run.bind_repaired(repaired(fixture))
        else:
            run.bind_author_candidates([fixture.records[1].key])
        assert run.snapshot()['pools'] == before


def test_concurrent_identical_explicit_requests_reuse_one_slot(fixture):
    from concurrent.futures import ThreadPoolExecutor
    with execution(fixture) as run:
        with run.stage('native'):
            with ThreadPoolExecutor(max_workers=4) as workers:
                jobs = list(workers.map(lambda _: queue_request(fixture, run), range(12)))
        assert len(set(jobs)) == 1
        assert len(run.snapshot()['operations']) == 1
        assert len(list(job_path(fixture, jobs[0]).parent.glob('*.job.json'))) == 1


def test_expired_deadline_during_durable_admission_publishes_no_job(fixture, monkeypatch):
    with execution(fixture) as run:
        original = run.admit
        def slow_admit(*args, **kwargs):
            result = original(*args, **kwargs)
            fixture.clock.advance(30)
            return result
        monkeypatch.setattr(run, 'admit', slow_admit)
        with run.stage('native'):
            with pytest.raises(BudgetExhausted):
                queue_request(fixture, run)
        assert len(run.snapshot()['operations']) == 1
        assert run.remaining('native') == 0
        assert not list((fixture.root / 'data' / 'writer-queue').glob('*.job.json'))


def test_queue_corruption_is_not_misclassified_as_writer_failure(fixture):
    op = operation(fixture, 'primary_writer', 'draft')
    with execution(fixture) as run:
        with run.stage('primary_writer'):
            job = queue_request(fixture, run, op=op)
        parent_writer.import_response(fixture.root, job, {'approved_later': True}, 'offline-writer')
        answer_path = job_path(fixture, job).with_name(f'{job}.answer.json')
        answer = read_json(answer_path)
        answer['response'] = {'unapproved': True}
        atomic_json(answer_path, answer)
        before = answer_path.read_bytes()
        with run.stage('primary_writer'):
            with pytest.raises(StateCorrupt):
                parent_writer.request(fixture.root, 'Frozen prompt', 20, execution=run, operation=op)
        assert run.snapshot()['writer_circuit'] is None
        assert answer_path.read_bytes() == before
        assert len(run.snapshot()['operations']) == 1


def test_missing_extension_fields_in_existing_journal_do_not_reset_budgets(fixture):
    with execution(fixture) as run:
        with run.stage('native'):
            fixture.clock.advance(4)
        payload, path = run.snapshot(), run.path
    del payload['repaired_bindings']
    del payload['author_candidates']
    write_sealed(path, payload)
    with execution(fixture) as run:
        assert run.remaining('native') == 26
        assert run.repaired_binding(fixture.records[0].key) is None
        assert run.author_candidates() is None
        run.bind_repaired(repaired(fixture))
        run.bind_author_candidates([])
        assert run.remaining('native') == 26


@pytest.mark.parametrize('write_reached_disk', [False, True])
def test_explicit_queue_write_failure_propagates_through_reading_without_new_slots(fixture, monkeypatch, write_reached_disk):
    from daily_agent.reading import read_papers
    original_atomic = parent_writer.atomic_json
    failure = OSError('Simulated queue journal persistence failure')

    def uncertain_write(path, value):
        if path.name.endswith('.job.json'):
            fixture.clock.advance(2)
            if write_reached_disk:
                original_atomic(path, value)
            raise failure
        return original_atomic(path, value)

    def invoke(prompt, timeout, **kwargs):
        return parent_writer.request(fixture.root, prompt, timeout, **kwargs)

    monkeypatch.setattr(parent_writer, 'atomic_json', uncertain_write)
    with execution(fixture) as run:
        with pytest.raises(StateCorrupt, match='queue persistence failed') as caught:
            read_papers([fixture.records[0]], fixture.config, invoke, execution=run)
        assert caught.value.__cause__ is failure
        state = run.snapshot()
        assert len(state['operations']) == 1
        op = next(iter(state['operations'].values()))
        assert op['identity'][-1] == 0
        assert state['writer_circuit'] is None
        assert run.remaining('native') == 28
        assert 'failures' not in fixture.records[0].reading
        assert job_path(fixture, op['queue_job_id']).exists() == write_reached_disk
    monkeypatch.setattr(parent_writer, 'atomic_json', original_atomic)
    with execution(fixture) as run:
        with pytest.raises(parent_writer.PendingResponse) as caught:
            read_papers([fixture.records[0]], fixture.config, invoke, execution=run)
        assert caught.value.job_id == op['queue_job_id']
        assert run.snapshot()['operations'] == state['operations']
        assert len(list(job_path(fixture, op['queue_job_id']).parent.glob('*.job.json'))) == 1
        assert run.remaining('native') == 28


@pytest.mark.parametrize('suffix', ['.active.json', '.job.json', '.answer.json'])
def test_explicit_queue_metadata_read_errors_are_state_errors(fixture, monkeypatch, suffix):
    original_read = parent_writer.read_json
    failure = OSError('Simulated queue metadata read failure')

    def failed_read(path):
        if path.name.endswith(suffix):
            raise failure
        return original_read(path)

    monkeypatch.setattr(parent_writer, 'read_json', failed_read)
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(StateCorrupt) as caught:
                parent_writer.request(fixture.root, 'Frozen prompt', 30, execution=run,
                                      operation=operation(fixture))
        assert caught.value.__cause__ is failure
        assert len(run.snapshot()['operations']) == int(suffix == '.answer.json')
        assert run.snapshot()['writer_circuit'] is None


@pytest.mark.parametrize('suffix', ['.job.json', '.claim.json'])
def test_pending_queue_scan_read_errors_propagate_without_admitting_replacement(fixture, monkeypatch, suffix):
    with pytest.raises(parent_writer.PendingResponse) as caught:
        parent_writer.request(fixture.root, 'Existing different prompt', 30)
    original_read = parent_writer.read_json
    failure = OSError('Simulated existing queue scan failure')
    target = job_path(fixture, caught.value.job_id).with_name(caught.value.job_id + suffix)

    def failed_read(path):
        if path == target:
            raise failure
        return original_read(path)

    monkeypatch.setattr(parent_writer, 'read_json', failed_read)
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(StateCorrupt) as failed:
                parent_writer.request(fixture.root, 'New frozen prompt', 30, execution=run,
                                      operation=operation(fixture))
        assert failed.value.__cause__ is failure
        assert not run.snapshot()['operations']
        assert len(list(target.parent.glob('*.job.json'))) == 1


def test_explicit_timeout_metadata_write_failure_keeps_existing_slot(fixture, monkeypatch):
    with execution(fixture) as run:
        with run.stage('native'):
            job = queue_request(fixture, run)
        before = deepcopy(run.snapshot()['operations'])
        saved = job_path(fixture, job).read_bytes()
        failure = OSError('Unable to persist bounded timeout')
        def failed_write(path, value):
            raise failure
        monkeypatch.setattr(parent_writer, 'atomic_json', failed_write)
        with run.stage('native'):
            fixture.clock.advance(1)
            with pytest.raises(StateCorrupt) as caught:
                queue_request(fixture, run)
        assert caught.value.__cause__ is failure
        assert run.snapshot()['operations'] == before
        assert job_path(fixture, job).read_bytes() == saved
        assert run.snapshot()['writer_circuit'] is None


@pytest.mark.parametrize('action', ['read', 'write'])
def test_legacy_queue_persistence_errors_retain_original_exception(fixture, monkeypatch, action):
    failure = OSError('Legacy failure remains an OSError')
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(parent_writer, 'read_json' if action == 'read' else 'atomic_json', fail)
    with pytest.raises(OSError) as caught:
        parent_writer.request(fixture.root, 'Legacy prompt', 30)
    assert caught.value is failure


def test_missing_source_image_is_not_reclassified_as_queue_corruption(fixture):
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(FileNotFoundError):
                parent_writer.request(fixture.root, 'Read source image', 30, str(fixture.root / 'missing.png'),
                                      execution=run, operation=operation(fixture))
        assert not run.snapshot()['operations']
        assert run.snapshot()['writer_circuit'] is None


def test_explicit_queue_lock_filesystem_error_preserves_cause(fixture, monkeypatch):
    failure = OSError('Cannot open the queue lock')
    @contextmanager
    def broken_lock(path):
        raise failure
        yield
    monkeypatch.setattr(parent_writer, 'exclusive_lock', broken_lock)
    with execution(fixture) as run:
        with run.stage('native'):
            with pytest.raises(StateCorrupt) as caught:
                queue_request(fixture, run)
        assert caught.value.__cause__ is failure
        assert not run.snapshot()['operations']
    assert parent_writer._QUEUE_THREADS.acquire(blocking=False)
    parent_writer._QUEUE_THREADS.release()
