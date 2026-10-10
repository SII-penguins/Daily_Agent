"""Offline integration checks for explicit shared execution at model stages.

All files and queues use tmp_path. No real model or network call is permitted.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from daily_agent import editorial, reading, visual_fidelity, visual_reading
from daily_agent.batch_execution import (BudgetExhausted, Execution, ExecutionConflict,
                                        candidate, digest)
from daily_agent.models import EditorialDraft, MaterialRecord
from daily_agent.parent_writer import PendingResponse, ExpiredResponse
from daily_agent.paper_document import version_identity


class StageClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


def stage_config(tmp_path, **reading_options):
    return SimpleNamespace(root=tmp_path, sources={
        'reading': {'run_budget_seconds': 30, 'visual_budget_seconds': 20,
                    'fidelity_budget_seconds': 40, 'max_chunks_per_paper': 10,
                    'timeout_seconds': 120, 'concurrent_reads': 1,
                    'fidelity_enabled': True, 'fidelity_detail_crops': False,
                    **reading_options},
        'llm_writer': {'provider': 'parent_queue', 'run_budget_seconds': 60,
                       'timeout_seconds': 120, 'batch_size': 2},
        'paper_text': {}, 'author_context': {},
    })


def stage_record(key='arxiv:2601.00001', *, kind='paper'):
    record = MaterialRecord(key=key, source='arxiv' if kind == 'paper' else 'github',
        item_type=kind, title='Frozen quantum routing evidence',
        url='https://arxiv.org/abs/' + key.split(':')[-1] + 'v1',
        abstract='A source-bounded quantum routing study')
    if kind == 'paper':
        text = 'A constrained quantum routing search reduces noise under a fixed baseline.'
        record.paper_document = {'identity': version_identity(record), 'content_hash': digest(text),
            'schema_version': 1, 'source_type': 'html', 'document_kind': 'full_text',
            'source_url': record.url,
            'chunks': [{'id': 'c1', 'text': text, 'page': 1}],
            'pages': [{'page': 1, 'text': text}]}
    return record


def stage_run(config, records, clock):
    return Execution(config.root / 'issue', 'original-batch', [candidate(r) for r in records],
                     config, protocol='explicit-stage-test-v1', clock=clock)


def admit_fake(execution, operation, prompt, *, role='reading', images=None):
    return execution.admit(*operation, {'prompt': prompt, 'images': images},
                           retry_generation=0, queue_role=role)


def valid_stage_note(record):
    chunk = record.paper_document['chunks'][0]
    return {'chunk_id': chunk['id'], 'summary': '本块说明有界量子线路搜索的证据。',
            'quotes': [chunk['text']], 'conditions': 'fixed baseline', 'evidence_kind': 'theory'}


def test_native_budget_is_shared_across_singletons_and_not_reset(tmp_path):
    config = stage_config(tmp_path, run_budget_seconds=10)
    records = [stage_record('arxiv:2601.0000' + str(i)) for i in range(1, 4)]
    clock, calls = StageClock(), []
    with stage_run(config, records, clock) as execution:
        def invoke(prompt, timeout, *, execution, operation):
            calls.append((timeout, operation))
            admit_fake(execution, operation, prompt)
            clock.advance(6)
            return valid_stage_note(next(r for r in records if r.key == operation[0]))
        reading.read_papers([records[0]], config, invoke, execution=execution)
        reading.read_papers([records[1]], config, invoke, execution=execution)
        reading.read_papers([records[2]], config, invoke, execution=execution)
        assert not records[2].reading['complete']
        assert records[2].reading['failures'][0]['reason'] == '阅读预算不足'
        assert [timeout for timeout, _ in calls] == [10, 4]
        assert execution.snapshot()['pools']['native']['charged'] == 10
        assert execution.snapshot()['pools']['native']['measured'] == 12
        assert execution.snapshot()['pools']['repaired']['remaining'] == 10


def test_native_repair_attempts_bind_exact_two_ordinals(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    operations = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, *, execution, operation):
            admit_fake(execution, operation, prompt)
            operations.append(operation)
            return {'bad': 'response'} if operation[-1] == 0 else valid_stage_note(record)
        reading.read_papers([record], config, invoke, execution=execution)
        assert record.reading['complete']
        assert operations == [(record.key, 'native', 'chunk:c1', 0),
                              (record.key, 'native', 'chunk:c1', 1)]
        assert execution.snapshot()['writer_circuit'] is None
        reading.read_papers([record], config, invoke, execution=execution)
        assert len(operations) == 2


@pytest.mark.parametrize('response_type', [PendingResponse, ExpiredResponse])
def test_reading_pending_and_expired_settle_without_writer_failure(tmp_path, response_type):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, *, execution, operation):
            admit_fake(execution, operation, prompt)
            clock.advance(3)
            raise response_type('test-job')
        with pytest.raises(response_type):
            reading.read_papers([record], config, invoke, execution=execution)
        state = execution.snapshot()
        assert state['pools']['native']['charged'] == 3
        assert state['writer_circuit'] is None and not state['reservations']


def image_stage_record(tmp_path):
    record = stage_record()
    image = tmp_path / 'page.png'
    image.write_bytes(b'fake offline page pixels')
    record.paper_document['pages'][0].update(visual_required=True, image_path=str(image),
        image_hash=hashlib.sha256(image.read_bytes()).hexdigest())
    return record


def test_visual_operation_is_material_page_bound(tmp_path):
    config, record, clock = stage_config(tmp_path), image_stage_record(tmp_path), StageClock()
    seen = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, image, *, execution, operation):
            admit_fake(execution, operation, prompt, role='review', images=image)
            seen.append(operation)
            clock.advance(4)
            return {'page': 1, 'summary': '逐页核对完成', 'figures': [], 'tables': [],
                    'formulas': [], 'text_matches_image': True, 'issues': []}
        visual_reading.read_visuals([record], config, invoke, execution=execution)
        assert seen == [(record.key, 'visual', 'page:1', 0)]
        state = execution.snapshot()
        assert state['pools']['visual']['charged'] == 4
        assert next(iter(state['operations'].values()))['queue_role'] == 'review'


def test_fidelity_distinct_transcribe_review_attempts_and_repaired_topology(tmp_path):
    config, record, clock = stage_config(tmp_path), image_stage_record(tmp_path), StageClock()
    original = deepcopy(record.paper_document)
    seen = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, image, *, execution, operation):
            role = 'review' if operation[2].startswith('review:') else 'draft'
            admit_fake(execution, operation, prompt, role=role, images=image)
            seen.append(operation)
            clock.advance(2)
            if role == 'draft':
                return {'page': 1, 'text': original['pages'][0]['text'], 'assets': [], 'unresolved': []}
            return {'page': 1, 'text_supported': operation[-1] == 1,
                    'inventory_complete': True, 'checks': [],
                    'issues': [] if operation[-1] == 1 else ['Check the original symbol again']}
        visual_fidelity.repair_visuals([record], config, invoke, execution=execution)
        assert [entry[2:] for entry in seen] == [('transcribe:1', 0), ('review:1', 0),
                                                ('transcribe:1', 1), ('review:1', 1)]
        assert record.paper_document['native_document'] == original
        assert record.reading['visual']['strict_fidelity']
        execution.bind_repaired(record)
        repaired_ids = []
        def read(prompt, timeout, *, execution, operation):
            admit_fake(execution, operation, prompt)
            repaired_ids.append(operation)
            chunk = json.loads(prompt.split('输入：\n')[1])
            return {'chunk_id': chunk['id'], 'summary': '保真文本的新分块。', 'quotes': [chunk['text']],
                    'conditions': 'fixed baseline', 'evidence_kind': 'theory'}
        reading.read_papers([record], config, read, execution=execution, phase='repaired')
        assert repaired_ids and all(op[1] == 'repaired' for op in repaired_ids)
        assert {op[2] for op in repaired_ids} == {'chunk:' + c['id'] for c in record.paper_document['chunks']}
        assert execution.snapshot()['pools']['fidelity']['charged'] == 8


@pytest.mark.parametrize('stage', ['native', 'visual', 'fidelity'])
def test_stage_identity_conflicts_never_become_soft_model_failure(tmp_path, stage):
    config, record, clock = stage_config(tmp_path), image_stage_record(tmp_path), StageClock()
    with stage_run(config, [record], clock) as execution:
        def conflict(*args, **kwargs):
            raise ExecutionConflict('Exact input changed')
        function = {'native': reading.read_papers, 'visual': visual_reading.read_visuals,
                    'fidelity': visual_fidelity.repair_visuals}[stage]
        with pytest.raises(ExecutionConflict):
            function([record], config, conflict, execution=execution)
        assert execution.snapshot()['writer_circuit'] is None


def test_draft_schema_failure_opens_original_batch_circuit_across_resume(tmp_path, monkeypatch):
    config, clock = stage_config(tmp_path), StageClock()
    records = [stage_record('github:example/one', kind='repo'), stage_record('github:example/two', kind='repo')]
    frozen = deepcopy(records)
    calls = []
    def request(root, prompt, timeout, *, stage, execution, operation):
        admit_fake(execution, operation, prompt, role=stage)
        calls.append(operation)
        clock.advance(5)
        return {'not': 'a draft array'}
    monkeypatch.setattr('daily_agent.parent_writer.request', request)
    with stage_run(config, frozen, clock) as execution:
        assert editorial._draft_with_llm_batch([records[0]], execution=execution,
            settings=editorial._llm_writer_settings(config)) == []
        assert execution.snapshot()['writer_circuit']['kind'] == 'schema'
    clock.advance(10000)  # Parent waiting is never charged as active stage time.
    with stage_run(config, frozen, clock) as execution:
        assert editorial._draft_with_llm_batch([records[1]], execution=execution,
            settings=editorial._llm_writer_settings(config)) == []
        assert execution.snapshot()['pools']['primary_writer']['charged'] == 5
    assert len(calls) == 1


@pytest.mark.parametrize('error, circuit', [
    (subprocess.TimeoutExpired('fake-model', 1), 'backend'),
    (json.JSONDecodeError('invalid JSON', '{', 0), 'json'),
    (TimeoutError('Writer queue busy'), None),
])
def test_only_actual_typed_primary_failures_open_circuit(tmp_path, monkeypatch, error, circuit):
    config, record, clock = stage_config(tmp_path), stage_record(kind='repo'), StageClock()
    def request(*args, **kwargs):
        raise error
    monkeypatch.setattr('daily_agent.parent_writer.request', request)
    with stage_run(config, [record], clock) as execution:
        assert editorial._draft_with_llm_batch([record], execution=execution,
            settings=editorial._llm_writer_settings(config)) == []
        assert (execution.snapshot()['writer_circuit'] or {}).get('kind') == circuit


@pytest.mark.parametrize('response_type', [PendingResponse, ExpiredResponse])
def test_primary_pending_settles_without_opening_circuit(tmp_path, monkeypatch, response_type):
    config, record, clock = stage_config(tmp_path), stage_record(kind='repo'), StageClock()
    def request(*args, **kwargs):
        clock.advance(7)
        raise response_type('test-job')
    monkeypatch.setattr('daily_agent.parent_writer.request', request)
    with stage_run(config, [record], clock) as execution:
        with pytest.raises(response_type):
            editorial._draft_with_llm_batch([record], execution=execution,
                                           settings=editorial._llm_writer_settings(config))
        state = execution.snapshot()
        assert state['pools']['primary_writer']['charged'] == 7
        assert state['writer_circuit'] is None and not state['reservations']


def semantic_stage_draft(record):
    return EditorialDraft(record.key, 'paper', record.title,
        {'problem': '研究有界搜索', 'confidence': 'medium'},
        claim_evidence=[{'field': 'problem', 'quote': record.paper_document['chunks'][0]['text']}],
        verification={'valid_fields': ['problem'], 'status': 'located', 'issues': []})


def test_semantic_attempts_have_two_distinct_slots_and_rejection_is_not_schema_failure(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    seen = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, *, execution, operation):
            admit_fake(execution, operation, prompt, role='review')
            seen.append(operation)
            clock.advance(3)
            return {'checks': [{'field': 'problem', 'supported': operation[-1] == 1,
                                'reason': 'Evidence checked'}]}
        for ordinal in (0, 1):
            draft = semantic_stage_draft(record)
            reading.semantic_review(draft, record, invoke, 120, execution=execution, ordinal=ordinal)
        assert [op[2:] for op in seen] == [('semantic', 0), ('semantic', 1)]
        assert execution.snapshot()['writer_circuit'] is None
        assert execution.snapshot()['pools']['primary_writer']['charged'] == 6


def test_semantic_missing_fields_opens_schema_circuit(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    with stage_run(config, [record], clock) as execution:
        draft = semantic_stage_draft(record)
        reading.semantic_review(draft, record, lambda *a, **k: {'checks': []}, 120, execution=execution)
        assert execution.snapshot()['writer_circuit']['kind'] == 'schema'
        assert draft.verification['semantic_support'] == 'review_failed'


def test_presentation_schema_failure_opens_circuit_and_conflicts_propagate(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    record.reading['claim_evidence'] = [{'field': 'key_result', 'quote': 'fixed baseline', 'conditions': 'fixed'}]
    draft = EditorialDraft(record.key, 'paper', record.title, {'key_result': '研究结果'})
    with stage_run(config, [record], clock) as execution:
        editorial._review_presentation(draft, record, lambda *a, **k: {'supported': True}, execution)
        assert execution.snapshot()['writer_circuit']['kind'] == 'schema'
        assert not record.reading['result_presentation']['passed']


def test_public_editorial_execution_forwards_to_real_parent_queue_and_settles(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(kind='repo'), StageClock()
    with stage_run(config, [record], clock) as execution:
        with pytest.raises(PendingResponse) as pending:
            editorial.draft_report_items(config, [record], execution=execution)
        operations = list(execution.snapshot()['operations'].values())
        assert len(operations) == 1
        assert operations[0]['identity'] == [record.key, 'primary_writer', 'draft', 0]
        assert operations[0]['queue_job_id'] == pending.value.job_id
        job = json.loads((tmp_path / 'data' / 'writer-queue' / (pending.value.job_id + '.job.json')).read_text())
        assert job['stage'] == 'draft' and job['call_timeout_seconds'] == 60
        assert not execution.snapshot()['reservations']


@pytest.mark.parametrize('phase,reason', [('visual', '视觉预算不足'), ('fidelity', 'FidelityBudgetExhausted')])
def test_exhausted_visual_pools_degrade_locally_without_model_calls(tmp_path, phase, reason):
    config = stage_config(tmp_path, **{phase + '_budget_seconds': 0})
    record, clock = image_stage_record(tmp_path), StageClock()
    calls = []
    with stage_run(config, [record], clock) as execution:
        function = visual_reading.read_visuals if phase == 'visual' else visual_fidelity.repair_visuals
        function([record], config, lambda *args, **kwargs: calls.append(args), execution=execution)
        visual = record.reading['visual']
        failure = visual['failures'][0] if phase == 'visual' else visual['fidelity']['pages'][0]
        assert failure['reason'] == reason
        assert not calls
        assert execution.remaining('native') == 30
        assert execution.remaining('primary_writer') == 60


def test_native_exhaustion_does_not_block_fidelity_and_repaired_reading(tmp_path):
    config, record, clock = stage_config(tmp_path), image_stage_record(tmp_path), StageClock()
    with stage_run(config, [record], clock) as execution:
        with execution.stage('native'):
            clock.advance(30)
        reading.read_papers([record], config, lambda *a, **kw: pytest.fail('exhausted native'), execution=execution)
        assert not record.reading['complete']
        def visual(prompt, timeout, image, *, execution, operation):
            if operation[2].startswith('transcribe:'):
                return {'page': 1, 'text': record.paper_document['pages'][0]['text'], 'assets': [], 'unresolved': []}
            return {'page': 1, 'text_supported': True, 'inventory_complete': True, 'checks': [], 'issues': []}
        visual_fidelity.repair_visuals([record], config, visual, execution=execution)
        execution.bind_repaired(record)
        def invoke(prompt, timeout, *, execution, operation):
            assert operation[1] == 'repaired'
            chunk = json.loads(prompt.split('输入：\n')[1])
            return {'chunk_id': chunk['id'], 'summary': '保真文本的新分块。', 'quotes': [chunk['text']],
                    'conditions': 'fixed baseline', 'evidence_kind': 'theory'}
        reading.read_papers([record], config, invoke, execution=execution, phase='repaired')
        assert record.reading['complete']
        assert execution.remaining('native') == 0
        assert execution.remaining('repaired') == 30


@pytest.mark.parametrize('error,kind', [(json.JSONDecodeError('bad', 'x', 0), 'json'),
                                      (subprocess.TimeoutExpired('model', 1), 'backend')])
def test_presentation_typed_model_failure_opens_shared_circuit(tmp_path, error, kind):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    record.reading['claim_evidence'] = [{'field': 'key_result', 'quote': 'fixed baseline', 'conditions': 'fixed'}]
    draft = EditorialDraft(record.key, 'paper', record.title, {'key_result': '研究结果'})
    def fail(*args, **kwargs):
        raise error
    with stage_run(config, [record], clock) as execution:
        editorial._review_presentation(draft, record, fail, execution)
        assert execution.snapshot()['writer_circuit']['kind'] == kind


def test_presentation_identity_conflict_cannot_be_swallowed_by_renderer(tmp_path):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    record.reading['claim_evidence'] = [{'field': 'key_result', 'quote': 'fixed baseline', 'conditions': 'fixed'}]
    draft = EditorialDraft(record.key, 'paper', record.title, {'key_result': '研究结果'})
    def fail(*args, **kwargs):
        raise ExecutionConflict('Changed exact prompt')
    with stage_run(config, [record], clock) as execution:
        with pytest.raises(ExecutionConflict):
            editorial._review_presentation(draft, record, fail, execution)
        assert execution.snapshot()['writer_circuit'] is None


def test_explicit_writer_settings_never_leak_from_legacy_context(tmp_path, monkeypatch):
    config, record, clock = stage_config(tmp_path, synthesis_chars=3), stage_record(), StageClock()
    record.reading['notes'] = [valid_stage_note(record)]
    seen = []
    def request(root, prompt, timeout, **kwargs):
        seen.append(json.loads(prompt.split('输入：\n')[1]))
        return []
    monkeypatch.setattr('daily_agent.parent_writer.request', request)
    token = editorial._LLM_WRITER_SETTINGS.set({'synthesis_chars': 500000})
    try:
        with stage_run(config, [record], clock) as execution:
            editorial._draft_with_llm_batch([record], execution=execution,
                                            settings=editorial._llm_writer_settings(config))
    finally:
        editorial._LLM_WRITER_SETTINGS.reset(token)
    assert seen[0][0]['reading_notes'] is None
    assert record.reading['synthesis_error']


@pytest.mark.parametrize('blocked', ['budget', 'circuit'])
def test_semantic_primary_blocking_degrades_without_calling_model(tmp_path, blocked):
    config, record, clock = stage_config(tmp_path), stage_record(), StageClock()
    with stage_run(config, [record], clock) as execution:
        if blocked == 'budget':
            with execution.stage('primary_writer'):
                clock.advance(60)
        else:
            execution.writer_failure('schema', 'Earlier material failed')
        draft = semantic_stage_draft(record)
        reading.semantic_review(draft, record, lambda *a, **k: pytest.fail('blocked'), 120, execution=execution)
        assert draft.verification['semantic_support'] == 'review_failed'
        assert draft.draft_fields['confidence'] == 'low'


def test_parallel_chunk_workers_share_explicit_execution_and_union_window(tmp_path):
    from threading import Barrier
    config, record, clock = stage_config(tmp_path, concurrent_reads=3), stage_record(), StageClock()
    record.paper_document['chunks'] = [{'id': 'c' + str(i), 'text': f'Independent source chunk {i} with actual bounded evidence.'}
                                       for i in range(1, 4)]
    barrier = Barrier(3, action=lambda: clock.advance(5))
    seen = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, *, execution, operation):
            admit_fake(execution, operation, prompt)
            seen.append((id(execution), timeout))
            barrier.wait(timeout=5)
            chunk = json.loads(prompt.split('输入：\n')[1])
            return {'chunk_id': chunk['id'], 'summary': '独立证据分块。', 'quotes': [chunk['text']],
                    'conditions': 'fixed baseline', 'evidence_kind': 'theory'}
        reading.read_papers([record], config, invoke, execution=execution)
        assert record.reading['complete']
        assert seen == [(id(execution), 30)] * 3
        state = execution.snapshot()
        assert state['pools']['native']['charged'] == 5
        assert len(state['operations']) == 3


@pytest.mark.parametrize('pool', ['native', 'visual'])
def test_subsecond_remaining_budget_reaches_transport_without_floor(tmp_path, pool):
    options = {'run_budget_seconds' if pool == 'native' else 'visual_budget_seconds': .05}
    config, record, clock = stage_config(tmp_path, **options), image_stage_record(tmp_path), StageClock()
    observed = []
    with stage_run(config, [record], clock) as execution:
        def invoke(prompt, timeout, *args, **kwargs):
            observed.append(timeout)
            if pool == 'native':
                return valid_stage_note(record)
            return {'page': 1, 'summary': '核对完成', 'figures': [], 'tables': [], 'formulas': [],
                    'text_matches_image': True, 'issues': []}
        function = reading.read_papers if pool == 'native' else visual_reading.read_visuals
        function([record], config, invoke, execution=execution)
        assert observed == [pytest.approx(.05)]


# Author/scientific additive stages use the same original execution.
from copy import deepcopy
import json
import pytest

from daily_agent.batch_execution import Execution, candidate, WriterCircuitOpen, ExecutionConflict, BudgetExhausted
from daily_agent.author_research import enrich_selected_author_contexts, validate_author_evidence
from daily_agent.parent_writer import PendingResponse, ExpiredResponse, pending, import_response, digest as author_digest
from daily_agent.scientific_analysis import analyze_papers
from test_author_research import setup as author_setup, source
from test_scientific_analysis import fixture as science_fixture


def normalized(config):
    config.sources.setdefault('reading', {}).update(run_budget_seconds=60, visual_budget_seconds=60, fidelity_budget_seconds=60, max_chunks_per_paper=12)
    config.sources.setdefault('llm_writer', {}).update(provider='parent_queue', run_budget_seconds=240)
    return config


def run(config, frozen, name='batch'):
    return Execution(config.root/'issue', name, frozen, config, protocol='offline-contract', clock=lambda:100)


def test_author_frozen_limit_singletons_full_evidence_and_resume(tmp_path):
    config, first=author_setup(tmp_path); normalized(config)
    config.sources['author_context']['max_research_papers_per_batch']=1
    second=deepcopy(first); second.key='pmlr:second'; second.title='Second Quantum Paper'
    frozen=[candidate(first),candidate(second)]
    with run(config,frozen) as execution:
        enrich_selected_author_contexts([second],config,execution=execution)
        assert execution.author_candidates()==[first.key]
        assert not pending(tmp_path)
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([first],config,execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
    job=pending(tmp_path)[0]
    assert job['stage']=='author_research'
    assert [row['key'] for row in json.loads(job['prompt'].split('INPUTS:\n')[1])]==[first.key]
    proposal={'papers':[{'key':first.key,'sources':[source(first)],'uncertainties':['Lab unknown']}]}
    import_response(tmp_path,job['job_id'],proposal,'researcher')
    with run(config,frozen) as execution:
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([first],config,execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
    review_job=pending(tmp_path)[0]
    assert review_job['stage']=='review'
    payload=json.loads(review_job['prompt'].split('\nREVIEW_INPUT:')[1])
    review={'input_sha256':author_digest(payload),'approved_source_hashes':[author_digest(source(first))],'uncertainties':{}}
    import_response(tmp_path,review_job['job_id'],review,'reviewer')
    with run(config,frozen) as execution:
        enrich_selected_author_contexts([first],config,execution=execution)
        enrich_selected_author_contexts([second],config,execution=execution)
        operations=[op['identity'] for op in execution.snapshot()['operations'].values()]
        assert {tuple(op) for op in operations}=={(first.key,'primary_writer','author_research',0),(first.key,'primary_writer','author_review',0)}
        evidence=first.raw['author_research_evidence']
        assert evidence['proposed']==proposal and evidence['review']==review
        assert validate_author_evidence(first,evidence)
        tampered=deepcopy(evidence);tampered['review']['approved_source_hashes']=['bogus']
        assert not validate_author_evidence(first,tampered)
        assert not validate_author_evidence(first,{'reviewed':True})
        enrich_selected_author_contexts([first],config,execution=execution)
    assert not pending(tmp_path)
    assert len(list((tmp_path/'data/writer-queue').glob('*.job.json')))==2


def test_author_complete_original_does_not_use_limit(tmp_path):
    config, complete=author_setup(tmp_path); normalized(config)
    config.sources['author_context']['max_research_papers_per_batch']=1
    complete.raw['research_context']={'authors':[], 'institutions':[], 'research_lines':[], 'labs':[{'name':'Explicit Lab','url':'https://lab.example.com','relationship':'paper_listed_by_group','evidence':[]}], 'uncertainties':[]}
    incomplete=deepcopy(complete);incomplete.key='pmlr:next';incomplete.raw={}
    frozen=[candidate(complete),candidate(incomplete)]
    with run(config,frozen) as execution:
        enrich_selected_author_contexts([complete],config,execution=execution)
        assert execution.author_candidates()==[incomplete.key]
        assert not pending(tmp_path)


@pytest.mark.parametrize('bad,kind', [({'papers':{}},None), (json.JSONDecodeError('bad','x',0),'json'), (ConnectionError('offline'),'backend')])
def test_author_circuit_classifies_model_failures(tmp_path,monkeypatch,bad,kind):
    config,record=author_setup(tmp_path);normalized(config);frozen=[candidate(record)]
    def fail(*args,**kwargs):
        if isinstance(bad,Exception):raise bad
        return bad
    monkeypatch.setattr('daily_agent.author_research.request',fail)
    with run(config,frozen) as execution:
        enrich_selected_author_contexts([record],config,execution=execution)
        assert (execution.snapshot()['writer_circuit'] or {}).get('kind')==kind
        assert record.raw['author_research_evidence']['status']=='not_reviewed'
        first=deepcopy(record.raw['author_research_evidence'])
        enrich_selected_author_contexts([record],config,execution=execution)
        assert record.raw['author_research_evidence'].get('proposed')==first.get('proposed')


@pytest.mark.parametrize('error',[PendingResponse('wait'),ExpiredResponse('late'),TimeoutError('queue'),ExecutionConflict('internal')])
def test_author_nonbackend_no_circuit(tmp_path,monkeypatch,error):
    config,record=author_setup(tmp_path);normalized(config);frozen=[candidate(record)]
    def fail(*args,**kwargs):raise error
    monkeypatch.setattr('daily_agent.author_research.request',fail)
    with run(config,frozen) as execution:
        if isinstance(error,TimeoutError):
            enrich_selected_author_contexts([record],config,execution=execution)
            assert record.raw['author_research_evidence']['status']=='not_reviewed'
        else:
            with pytest.raises(type(error)):enrich_selected_author_contexts([record],config,execution=execution)
        assert execution.snapshot()['writer_circuit'] is None


def test_author_valid_independent_rejection_retains_full_evidence(tmp_path,monkeypatch):
    config,record=author_setup(tmp_path);normalized(config);frozen=[candidate(record)]
    def answer(root,prompt,timeout,**kwargs):
        assert kwargs['execution'] is execution
        if kwargs['stage']=='author_research':return {'papers':[{'key':record.key,'sources':[source(record)],'uncertainties':[]}]}
        payload=json.loads(prompt.split('\nREVIEW_INPUT:')[1])
        return {'input_sha256':author_digest(payload),'approved_source_hashes':[],'uncertainties':{record.key:['Unsupported lab claim']}}
    monkeypatch.setattr('daily_agent.author_research.request',answer)
    with run(config,frozen) as execution:
        enrich_selected_author_contexts([record],config,execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
        assert record.raw['author_research_sources']==[]
        assert validate_author_evidence(record,record.raw['author_research_evidence'])


def test_science_real_queue_operations(tmp_path):
    config,record,draft,analysis,_,_,invoke,_=science_fixture(tmp_path);normalized(config);frozen=[candidate(record)]
    with run(config,frozen) as execution:
        with pytest.raises(PendingResponse):analyze_papers(config,[record],[draft],execution=execution)
    job=pending(tmp_path)[0]; assert job['stage']=='draft'
    import_response(tmp_path,job['job_id'],analysis,'science-author')
    with run(config,frozen) as execution:
        with pytest.raises(PendingResponse):analyze_papers(config,[record],[draft],execution=execution)
    job=pending(tmp_path)[0]; assert job['stage']=='review'
    answer=invoke(job['prompt'],30,stage='review')
    import_response(tmp_path,job['job_id'],answer,'science-reviewer')
    with run(config,frozen) as execution:
        analyze_papers(config,[record],[draft],execution=execution)
        assert record.reading['scientific_analysis']['status']=='passed'
        identities={tuple(value['identity']) for value in execution.snapshot()['operations'].values()}
        assert identities=={(record.key,'primary_writer','scientific_writer',0),(record.key,'primary_writer','scientific_review',0)}
        assert execution.snapshot()['writer_circuit'] is None


@pytest.mark.parametrize('mode,kind',[('schema',None),('review_schema',None),('reject',None),('json','json'),('backend','backend'),('queue',None)])
def test_science_failure_classification(tmp_path,mode,kind):
    config,record,draft,analysis,_,_,invoke,_=science_fixture(tmp_path);normalized(config);frozen=[candidate(record)]
    def respond(prompt,timeout,*,stage):
        if mode=='json':raise json.JSONDecodeError('bad','x',0)
        if mode=='backend':raise ConnectionError('offline')
        if mode=='queue':raise TimeoutError('queue waiting')
        if mode=='schema':return {'bad':True}
        response=invoke(prompt,timeout,stage=stage)
        if stage=='review':
            if mode=='review_schema':response['checks'][0]['supported']='false'
            elif mode=='reject':response['checks'][0]['supported']=False
        return response
    with run(config,frozen) as execution:
        analyze_papers(config,[record],[draft],execution=execution,invoke=respond)
        circuit=execution.snapshot()['writer_circuit']
        assert (circuit['kind'] if circuit else None)==kind
        assert record.reading['scientific_analysis']['status']==('not_reviewed' if mode=='queue' else 'failed')
        if mode=='queue':assert not list((tmp_path/'data/scientific-analysis').glob('*/result.json'))


def test_science_expiry_propagates_explicitly_without_circuit(tmp_path):
    config,record,draft,*_=science_fixture(tmp_path);normalized(config);frozen=[candidate(record)]
    def expired(*args,**kwargs):raise ExpiredResponse('science-expired')
    with run(config,frozen) as execution:
        with pytest.raises(ExpiredResponse):analyze_papers(config,[record],[draft],execution=execution,invoke=expired)
        assert execution.snapshot()['writer_circuit'] is None


def test_author_old_reviewed_flag_cache_does_not_authorize_explicit_reuse(tmp_path):
    config,record=author_setup(tmp_path);normalized(config);frozen=[candidate(record)]
    old_identity={'key':record.key,'title':record.title,'doi':record.doi,'authors':record.authors,
        'schema_version':1,'registry_hash':author_digest([]),'document_hash':record.paper_document.get('content_hash'),
        'frontmatter':record.paper_document['pages']}
    folder=tmp_path/'data/author_context/research-v1';folder.mkdir(parents=True)
    (folder/(author_digest(old_identity)+'.json')).write_text(json.dumps({'identity':old_identity,'reviewed':True,'sources':[source(record)]}))
    with run(config,frozen) as execution:
        with pytest.raises(PendingResponse):enrich_selected_author_contexts([record],config,execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
    assert len(pending(tmp_path))==1


def test_author_deadline_between_writer_and_review_does_not_open_circuit(tmp_path,monkeypatch):
    config,record=author_setup(tmp_path);normalized(config);frozen=[candidate(record)]
    config.sources['llm_writer']['run_budget_seconds']=4
    now=[100.0];calls=[]
    def answer(root,prompt,timeout,**kwargs):
        calls.append((kwargs['stage'],timeout));now[0]+=5
        return {'papers':[{'key':record.key,'sources':[source(record)],'uncertainties':[]}]}
    monkeypatch.setattr('daily_agent.author_research.request',answer)
    with Execution(config.root/'issue','batch',frozen,config,protocol='offline-contract',clock=lambda:now[0]) as execution:
        enrich_selected_author_contexts([record],config,execution=execution)
        assert record.raw['author_research_evidence']['status']=='not_reviewed'
        assert 'proposed' in record.raw['author_research_evidence']
        assert execution.snapshot()['writer_circuit'] is None
    assert calls==[('author_research',4)]


def test_science_deadline_between_writer_and_review_does_not_open_circuit(tmp_path):
    config,record,draft,analysis,*_=science_fixture(tmp_path);normalized(config);frozen=[candidate(record)]
    config.sources['llm_writer']['run_budget_seconds']=4
    now=[100.0];calls=[]
    def answer(prompt,timeout,*,stage):
        calls.append((stage,timeout));now[0]+=5
        return analysis
    with Execution(config.root/'issue','batch',frozen,config,protocol='offline-contract',clock=lambda:now[0]) as execution:
        analyze_papers(config,[record],[draft],execution=execution,invoke=answer)
        assert record.reading['scientific_analysis']['status']=='not_reviewed'
        assert not list((tmp_path/'data/scientific-analysis').glob('*/result.json'))
        assert execution.snapshot()['writer_circuit'] is None
    assert calls==[('draft',4)]


def poison_stage_admission(monkeypatch, execution, *, after_write=False, error_type=OSError):
    from daily_agent import batch_execution
    original = batch_execution.atomic_json
    def fail(path, value):
        is_admission = Path(path) == execution.path and bool(value['payload'].get('operations'))
        if is_admission and not after_write:
            raise error_type('Injected uncertain admission persistence')
        result = original(path, value)
        if is_admission:
            raise error_type('Injected uncertain admission persistence')
        return result
    monkeypatch.setattr(batch_execution, 'atomic_json', fail)


@pytest.mark.parametrize('stage', ['native', 'visual', 'fidelity', 'draft', 'semantic', 'presentation', 'science', 'author'])
@pytest.mark.parametrize('after_write', [False, True])
def test_poisoned_admission_write_is_never_downgraded_or_followed_by_model_work(tmp_path, monkeypatch, stage, after_write):
    from daily_agent.parent_writer import request
    if stage == 'science':
        config, record, draft, *_ = science_fixture(tmp_path)
        normalized(config)
    elif stage == 'author':
        config, record = author_setup(tmp_path)
        normalized(config)
    else:
        config, record = stage_config(tmp_path), image_stage_record(tmp_path)
    clock = StageClock()
    with stage_run(config, [record], clock) as execution:
        poison_stage_admission(monkeypatch, execution, after_write=after_write)
        def invoke(prompt, timeout, image_path=None, **kwargs):
            return request(config.root, prompt, timeout, image_path, **kwargs)
        with pytest.raises((ExecutionConflict, OSError)):
            if stage == 'native':
                reading.read_papers([record], config, invoke, execution=execution)
            elif stage == 'visual':
                visual_reading.read_visuals([record], config, invoke, execution=execution)
            elif stage == 'fidelity':
                visual_fidelity.repair_visuals([record], config, invoke, execution=execution)
            elif stage == 'draft':
                editorial._draft_with_llm_batch([record], execution=execution,
                                                settings=editorial._llm_writer_settings(config))
            elif stage == 'semantic':
                reading.semantic_review(semantic_stage_draft(record), record, invoke, 120, execution=execution)
            elif stage == 'presentation':
                record.reading['claim_evidence'] = [{'field': 'key_result', 'quote': 'baseline', 'conditions': 'fixed'}]
                draft = EditorialDraft(record.key, 'paper', record.title, {'key_result': '研究结果'})
                editorial._review_presentation(draft, record, invoke, execution)
            elif stage == 'science':
                analyze_papers(config, [record], [draft], execution=execution)
            else:
                enrich_selected_author_contexts([record], config, execution=execution)
        assert execution._poisoned
        assert execution._state['writer_circuit'] is None
        assert len(execution._state['reservations']) == 1
        assert not execution._windows
        assert not list((tmp_path / 'data' / 'writer-queue').glob('*.job.json'))
        with pytest.raises(ExecutionConflict):
            execution.snapshot()


@pytest.mark.parametrize('stage', ['native', 'visual', 'fidelity'])
def test_healthy_ledger_keeps_ordinary_transport_filesystem_failure_bounded(tmp_path, stage):
    config, record, clock = stage_config(tmp_path), image_stage_record(tmp_path), StageClock()
    def missing_cache(*args, **kwargs):
        raise OSError('Ordinary transport dependency unavailable')
    with stage_run(config, [record], clock) as execution:
        function = {'native': reading.read_papers, 'visual': visual_reading.read_visuals,
                    'fidelity': visual_fidelity.repair_visuals}[stage]
        function([record], config, missing_cache, execution=execution)
        state = execution.snapshot()
        assert state['writer_circuit'] is None and not state['reservations']


def test_author_timeout_shaped_ledger_write_failure_cannot_be_degraded(tmp_path, monkeypatch):
    config, record = author_setup(tmp_path)
    normalized(config)
    with stage_run(config, [record], StageClock()) as execution:
        poison_stage_admission(monkeypatch, execution, error_type=TimeoutError)
        with pytest.raises(ExecutionConflict):
            enrich_selected_author_contexts([record], config, execution=execution)
        assert execution._poisoned
