"""Strict negative-only stop tests with actual offline queue/claim receipts."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
import hashlib
import json
from types import SimpleNamespace

import fitz
import pytest

from daily_agent import editorial, parent_writer as queue, pipeline, visual_fidelity, visual_reading
from daily_agent.batch_execution import Execution, candidate, ExecutionConflict
from daily_agent.cloud_workflow import PROFILE
from daily_agent.incremental_issue import PROTOCOL
from daily_agent.paper_document import attach_document, build_document, digest
from daily_agent.reading import audit_reading
from daily_agent.terminal_fidelity import terminal_rejection, TerminalFidelityRejected
from daily_agent.visual_inventory import read_visual_evidence
from daily_agent.workflow_state import atomic_json, read_json, StateCorrupt
from test_fidelity_first import fixture, StageClock
from test_visual_inventory import OfflineEvidence


def strict_fixture(root, count=2):
    cfg, record = fixture(root, count)
    cfg.delivery = {'cloud': {'profile': PROFILE}}
    cfg.quota = {'paper_target': 8, 'github_target': 2, 'max_items': 10}
    pdf_path = root/'paper.pdf'
    with fitz.open() as pdf:
        for page in record.paper_document['pages']:
            p = pdf.new_page(width=200, height=200)
            p.insert_text((20, 30), 'Original source page '+str(page['page']))
        pdf.save(pdf_path)
    pages = deepcopy(record.paper_document['pages'])
    with fitz.open(pdf_path) as pdf:
        for page in pages:
            image = root/f"page-{page['page']}.png"
            pdf[page['page']-1].get_pixmap().save(image)
            page.update(image_path=str(image), image_hash=hashlib.sha256(image.read_bytes()).hexdigest())
    document = build_document(record, pages, record.url, 'pdf', cfg.sources['paper_text'])
    document.update(source_pdf_path=str(pdf_path), source_pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(), source_page_count=count)
    attach_document(record, document)
    return cfg, record


def execution(cfg, originals, clock):
    return Execution(cfg.root/'issue', 'batch', [candidate(r) for r in originals], cfg,
                     protocol=PROTOCOL, clock=clock)


def invoke(cfg):
    return lambda prompt, timeout, image=None, **kwargs: queue.request(cfg.root, prompt, timeout, image, **kwargs)


def answer(cfg, pending, responder):
    job = read_json(queue._folder(cfg.root)/f'{pending.job_id}.job.json')
    response = responder(job['prompt'], 10)
    worker = 'independent-reviewer' if job['stage'] == 'review' else 'page-reader'
    claim = queue.claim(cfg.root, pending.job_id, worker)
    queue.import_response(cfg.root, pending.job_id, response, worker, claim_token=claim['token'])


def finish(cfg, original, clock, responder, *, should_stop=True):
    for _ in range(100):
        record = deepcopy(original)
        try:
            with execution(cfg, [original], clock) as ex:
                read_visual_evidence([record], cfg, invoke(cfg), execution=ex)
        except queue.PendingResponse as pending:
            answer(cfg, pending, responder)
        except TerminalFidelityRejected:
            assert should_stop
            return record
        else:
            assert not should_stop
            return record
    pytest.fail('Finite offline evidence sequence did not terminate')


def test_terminal_strict_pdf_adds_no_visual_native_writer_or_science_jobs(tmp_path, monkeypatch):
    cfg, original = strict_fixture(tmp_path)
    clock = StageClock(); responder = OfflineEvidence(clock, fail_pages=(1,))
    record = finish(cfg, original, clock, responder)
    jobs = list(queue._folder(tmp_path).glob('*.job.json'))
    assert len(jobs) == 5  # identical retry candidate reuses its exact review job
    assert record.reading['blocked']['failed_pages'] == [1]
    assert record.reading['blocked']['required_pages'] == 2
    assert not record.reading['complete'] and not record.reading['visual']['strict_fidelity']
    assert record.reading['unread_chunk_ids'] == [c['id'] for c in original.paper_document['chunks']]
    assert not record.paper_text_status['sufficient_for_deep_summary']
    assert audit_reading([record.to_dict()])['full_text_read_count'] == 0
    monkeypatch.setattr(editorial, '_draft_with_llm', lambda *a, **k: pytest.fail('writer cannot run'))
    with execution(cfg, [original], clock) as ex:
        with pytest.raises(TerminalFidelityRejected):
            editorial._draft_report_items_uncached(cfg, [deepcopy(original)], execution=ex)
        state = ex.snapshot()
        assert not state['completed'] and not state['reservations']
        assert set(op['identity'][1] for op in state['operations'].values()) == {'fidelity'}
        for name in ('native', 'repaired', 'visual', 'primary_writer'):
            assert state['pools'][name]['charged'] == 0
    assert list(queue._folder(tmp_path).glob('*.job.json')) == jobs


@pytest.mark.parametrize('mode', ['noncloud', 'noexecution'])
def test_legacy_and_degraded_paths_keep_display_fallback(tmp_path, mode):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    if mode == 'noncloud': cfg.delivery = {}
    if mode == 'noexecution':
        responder = OfflineEvidence(clock, fail_pages=(1,))
        record = deepcopy(original)
        read_visual_evidence([record], cfg, responder)
        assert responder.calls.count('visual') == 2
    else:
        record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)), should_stop=False)
        jobs = [read_json(p) for p in queue._folder(tmp_path).glob('*.job.json')]
        assert sum(j['prompt'].startswith('核对附带') for j in jobs) == 2
    assert 'blocked' not in record.reading
    assert record.reading['visual']['complete'] and record.reading['visual']['strict_fidelity'] is False


def test_exact_existing_visual_notes_are_retained(tmp_path):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    fake = OfflineEvidence(clock)
    visual_reading.read_visuals([original], cfg, fake)
    old = deepcopy(original.reading['visual'])
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    assert record.reading['visual']['notes'] == old['notes']
    assert record.reading['visual']['complete'] == old['complete']
    assert not record.reading['complete']


@pytest.mark.parametrize('change', ['partial', 'page_gap', 'pdf_hash', 'pdf_bytes', 'missing_pdf', 'image_hash',
                                   'settings', 'attempt_missing', 'candidate_corrupt', 'review_corrupt',
                                   'receipt_corrupt', 'claim_corrupt', 'same_worker', 'candidate_uncertain'])
def test_uncertain_or_changed_evidence_cannot_become_terminal(tmp_path, change):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    with execution(cfg, [original], clock) as ex:
        key = next(p for p in (tmp_path/'data/reading/fidelity').glob('*.attempt-2.json'))
        saved = read_json(key)
        if change == 'partial': record.paper_document['document_kind'] = 'partial_text'
        elif change == 'page_gap': record.paper_document['pages'].pop()
        elif change == 'pdf_hash': record.paper_document['source_pdf_sha256'] = 'e'*64
        elif change == 'missing_pdf': (tmp_path/'paper.pdf').unlink()
        elif change == 'pdf_bytes': (tmp_path/'paper.pdf').write_bytes(b'changed')
        elif change == 'image_hash': (tmp_path/'page-1.png').write_bytes(b'changed')
        elif change == 'settings': cfg.sources['reading']['new_evidence_setting'] = True
        elif change == 'attempt_missing': key.unlink()
        elif change == 'candidate_corrupt': saved['candidate']['text'] += 'changed'; atomic_json(key, saved)
        elif change == 'review_corrupt': saved['review']['checks'] = []; atomic_json(key, saved)
        elif change == 'candidate_uncertain':
            saved['review'].update(issues=[], text_supported=True, inventory_complete=True)
            for check in saved['review']['checks']: check['supported'] = True
            atomic_json(key, saved)
        else:
            receipt = record.reading['blocked']['pages'][0]['attempts'][1]
            job = receipt['review_job_id']
            path = queue._folder(tmp_path)/f'{job}.answer.json'
            value = read_json(path)
            if change == 'same_worker':
                value['worker_id'] = 'page-reader'; atomic_json(path, value)
                claim_path = queue._folder(tmp_path)/f'{job}.claim.json'
                claim = read_json(claim_path); claim['worker_id'] = 'page-reader'; atomic_json(claim_path, claim)
            elif change == 'receipt_corrupt':
                value['response_sha256'] = 'e'*64; atomic_json(path, value)
            elif change == 'claim_corrupt':
                path = queue._folder(tmp_path)/f'{job}.claim.json'
                value = read_json(path); value['worker_id'] = 'wrong'; atomic_json(path, value)
        if change in {'receipt_corrupt', 'claim_corrupt'}:
            with pytest.raises(StateCorrupt): terminal_rejection(record, cfg, ex)
        else:
            assert terminal_rejection(record, cfg, ex) is None
        assert not ex.snapshot()['completed']


@pytest.mark.parametrize('state', ['pending', 'expired', 'busy'])
def test_fidelity_receipts_preserve_pending_expiry_and_busy(tmp_path, monkeypatch, state):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    receipt = record.reading['blocked']['pages'][0]['attempts'][1]
    job_id = receipt['review_job_id']
    folder = queue._folder(tmp_path)
    (folder/f'{job_id}.answer.json').unlink()
    if state == 'expired':
        job = read_json(folder/f'{job_id}.job.json')
        job['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        atomic_json(folder/f'{job_id}.job.json', job)
    if state == 'busy':
        @contextmanager
        def busy(*a, **k):
            raise TimeoutError('offline lock contention')
            yield
        monkeypatch.setattr(queue, 'queue_lock', busy)
    with execution(cfg, [original], clock) as ex:
        with pytest.raises(queue.PendingResponse) as pending:
            terminal_rejection(record, cfg, ex)
        assert bool(getattr(pending.value, 'expired', False)) == (state == 'expired')
        assert not ex.snapshot()['completed']


@pytest.mark.parametrize('state', ['pending', 'expired', 'answered', 'retired', 'corrupt'])
def test_bound_visual_jobs_are_never_hidden_or_replaced(tmp_path, state):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    with execution(cfg, [original], clock) as ex:
        with pytest.raises(queue.PendingResponse) as pending:
            visual_reading.read_visuals([deepcopy(original)], cfg, invoke(cfg), execution=ex)
    job_id = pending.value.job_id; folder = queue._folder(tmp_path)
    if state in {'answered', 'corrupt'}:
        answer(cfg, pending.value, OfflineEvidence(clock))
        if state == 'corrupt':
            path = folder/f'{job_id}.answer.json'
            value = read_json(path); value['response_sha256'] = 'e'*64; atomic_json(path, value)
    elif state == 'expired':
        job = read_json(folder/f'{job_id}.job.json')
        job['expires_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        atomic_json(folder/f'{job_id}.job.json', job)
    elif state == 'retired':
        atomic_json(folder/f'{job_id}.active.json', {'base_job_id': job_id, 'job_id': 'e'*64})
    jobs = set(folder.glob('*.job.json'))
    with execution(cfg, [original], clock) as ex:
        error = ExecutionConflict if state in {'retired', 'corrupt'} else TerminalFidelityRejected if state == 'answered' else queue.PendingResponse
        with pytest.raises(error):
            read_visual_evidence([deepcopy(original)], cfg, invoke(cfg), execution=ex)
        assert not ex.snapshot()['completed']
    assert set(folder.glob('*.job.json')) == jobs


def test_pipeline_continues_good_sibling_without_completion_for_rejected(tmp_path, monkeypatch):
    from daily_agent import author_context, scientific_analysis
    from daily_agent.models import EditorialDraft
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    # Produce original terminal receipts under the real two-member contract.
    good = deepcopy(original); good.key = 'arxiv:good-sibling'; good.title = 'Good sibling'
    originals = [original, good]
    plan = SimpleNamespace(folder=tmp_path/'issue')
    responder = OfflineEvidence(clock, fail_pages=(1,))
    for _ in range(100):
        record = deepcopy(original)
        try:
            with execution(cfg, originals, clock) as ex:
                read_visual_evidence([record], cfg, invoke(cfg), execution=ex)
        except queue.PendingResponse as pending: answer(cfg, pending, responder)
        except TerminalFidelityRejected: break
    else: pytest.fail('terminal receipt setup did not finish')
    calls = []
    monkeypatch.setattr(author_context, 'enrich_selected_author_contexts', lambda *a, **k: None)
    monkeypatch.setattr(scientific_analysis, 'analyze_papers', lambda config, records, *a, **k: calls.append(('science', records[0].key)))
    def draft(config, records, **kwargs):
        r = records[0]
        if r.key == original.key:
            return editorial._draft_report_items_uncached(config, records, **kwargs)
        calls.append(('writer', r.key))
        return [EditorialDraft(r.key, r.item_type, r.title, {})]
    monkeypatch.setattr(pipeline, 'draft_report_items', draft)
    monkeypatch.setattr(pipeline, 'write_material_library', lambda *a: None)
    monkeypatch.setattr(pipeline, 'write_editorial_artifacts', lambda *a: None)
    library = {}
    jobs = set(queue._folder(tmp_path).glob('*.job.json'))
    records, drafts, reviews = pipeline._process_incremental_batch(cfg, date(2026,10,9), deepcopy(originals),
                        plan, 'batch', library, [], [], [], use_llm=True)
    assert calls == [('writer', good.key), ('science', good.key)]
    assert [r.key for r in records] == [r.key for r in originals]
    assert records[0].reading['blocked']['state'] == 'blocked' and reviews[0].verdict == 'FAIL'
    assert pipeline.approve_publication(cfg, records, drafts, reviews) == []
    assert not read_json(next((plan.folder/'batch-execution').glob('*/journal.json')))['payload']['completed']
    assert set(queue._folder(tmp_path).glob('*.job.json')) == jobs


def test_twenty_page_rejection_retains_eighteen_accepted_and_zero_reading(tmp_path):
    cfg, original = strict_fixture(tmp_path, 20); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(13, 14)))
    manifest = record.reading['visual']['fidelity']
    assert sum(p['passed'] for p in manifest['pages']) == 18
    assert record.reading['blocked']['failed_pages'] == [13, 14]
    assert record.reading.get('notes', []) == [] and record.reading['coverage'] == 0
    assert len(record.reading['unread_chunk_ids']) == len(original.paper_document['chunks'])
    jobs = [read_json(p) for p in queue._folder(tmp_path).glob('*.job.json')]
    assert len(jobs) == 42  # 40 first attempts + 2 finite repair transcriptions
    assert all(j['prompt'].startswith(('你是论文页面转写员', '你是独立的页面保真复核员')) for j in jobs)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
              for pattern in ('*.job.json', '*.answer.json') for p in queue._folder(tmp_path).glob(pattern)}
    with execution(cfg, [original], clock) as ex:
        with pytest.raises(TerminalFidelityRejected):
            read_visual_evidence([deepcopy(original)], cfg, invoke(cfg), execution=ex)
    assert before == {p: hashlib.sha256(__import__('pathlib').Path(p).read_bytes()).hexdigest() for p in before}


def test_retry_with_real_detail_crops_is_bound_to_both_receipts(tmp_path):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    cfg.sources['reading']['fidelity_detail_crops'] = True
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    attempt = record.reading['blocked']['pages'][0]['attempts'][1]
    job = read_json(queue._folder(tmp_path)/f"{attempt['transcribe_job_id']}.job.json")
    assert len(job['images']) in {5, 9}
    assert record.reading['blocked']['failed_pages'] == [1]


def test_first_rejection_still_admits_original_finite_second_attempt(tmp_path):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    responder = OfflineEvidence(clock, fail_pages=(1,))
    for _ in range(3):
        with execution(cfg, [original], clock) as ex:
            with pytest.raises(queue.PendingResponse) as pending:
                read_visual_evidence([deepcopy(original)], cfg, invoke(cfg), execution=ex)
        job = read_json(queue._folder(tmp_path)/f'{pending.value.job_id}.job.json')
        if _ == 2:
            payload = json.loads(job['prompt'].split('输入：\n', 1)[1])
            assert payload['previous_review']['text_supported'] is False
            assert job['prompt'].startswith('你是论文页面转写员')
            break
        answer(cfg, pending.value, responder)


@pytest.mark.parametrize('mode', ['disabled', 'budget', 'invalid_review', 'source_gap'])
def test_no_terminal_shortcut_for_nonterminal_failure_shapes(tmp_path, mode):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    if mode == 'disabled': cfg.sources['reading']['visual_enabled'] = False
    elif mode == 'budget': cfg.sources['reading']['fidelity_budget_seconds'] = 0
    elif mode == 'source_gap': original.paper_document['source_page_count'] += 1
    responder = OfflineEvidence(clock, fail_pages=(1,))
    def response(prompt, *a, **k):
        result = responder(prompt, *a, **k)
        if mode == 'invalid_review' and prompt.startswith('你是独立'): result['checks'] = 'invalid'
        return result
    record = finish(cfg, original, clock, response, should_stop=False)
    assert 'blocked' not in record.reading and record.reading['visual']['strict_fidelity'] is False


def test_terminal_observation_never_absorbs_workflow_cancellation(tmp_path, monkeypatch):
    from daily_agent.workflow_runtime import WorkflowCancelled
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    @contextmanager
    def cancelled(*a, **k):
        raise WorkflowCancelled('offline cancellation')
        yield
    monkeypatch.setattr(queue, 'queue_lock', cancelled)
    with execution(cfg, [original], clock) as ex:
        with pytest.raises(WorkflowCancelled): terminal_rejection(record, cfg, ex)


def test_strict_pilot_uses_same_negative_gate_as_cloud_delivery(tmp_path):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    cfg.delivery['cloud'].update(pilot=True, require_full_reading=True)
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    assert record.reading['blocked']['failed_pages'] == [1]
    assert not record.reading['complete']
    assert all(not read_json(p)['prompt'].startswith('核对附带') for p in queue._folder(tmp_path).glob('*.job.json'))


@pytest.mark.parametrize('actual_read', [False, True])
def test_reading_coverage_requires_exact_reader_cache_provenance(tmp_path, actual_read):
    from daily_agent import reading
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    def read(prompt, timeout, **kwargs):
        chunk = json.loads(prompt.split('输入：\n', 1)[1])
        return {'chunk_id': chunk['id'], 'summary': '逐字原文提供限定条件下的证据。',
                'quotes': [chunk['text'][:100]], 'conditions': 'fixed baseline', 'evidence_kind': 'simulation'}
    if actual_read:
        reading.read_papers([original], cfg, read)
        assert original.reading['complete']
        expected_notes = deepcopy(original.reading['notes'])
    else:
        chunk = original.paper_document['chunks'][0]
        stale = {'chunk_id': chunk['id'], 'summary': '旧文笔记恰好匹配共同原句。',
                 'quotes': [chunk['text'][:100]], 'conditions': 'fixed baseline', 'evidence_kind': 'simulation'}
        assert reading.valid_note(stale, chunk, cfg.sources['reading'])
        original.reading = {'fingerprint': 'different-source', 'notes': [stale],
                            'read_chunk_ids': [chunk['id']], 'coverage': 1, 'complete': True}
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    assert record.reading['complete'] is actual_read
    assert record.reading['coverage'] == (1 if actual_read else 0)
    if actual_read:
        assert record.reading['notes'] == expected_notes
        assert record.reading['unread_chunk_ids'] == []
    else:
        assert record.reading['notes'] == []
        assert record.reading['read_chunk_ids'] == []
    assert audit_reading([record.to_dict()])['quality_passed_count'] == 0
    assert not record.paper_text_status['sufficient_for_deep_summary']


def test_partial_exact_visual_notes_are_retained_without_completion(tmp_path):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    shadow = deepcopy(original)
    shadow.paper_document['pages'] = shadow.paper_document['pages'][:1]
    visual_reading.read_visuals([shadow], cfg, OfflineEvidence(clock))
    note = deepcopy(shadow.reading['visual']['notes'][0])
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    visual = record.reading['visual']
    assert visual['notes'] == [note]
    assert not visual['complete'] and not visual['passed'] and not visual['strict_fidelity']
    assert visual['required_pages'] == 2 and not record.reading['complete']


@pytest.mark.parametrize('role', ['transcribe', 'review'])
def test_missing_initial_claim_declines_shortcut_and_retains_legacy_fallback(tmp_path, role):
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    receipt = record.reading['blocked']['pages'][0]['attempts'][1]
    job_id = receipt[role+'_job_id']; folder = queue._folder(tmp_path)
    (folder/f'{job_id}.claim.json').unlink()
    before = set(folder.glob('*.job.json'))
    with execution(cfg, [original], clock) as ex:
        assert terminal_rejection(record, cfg, ex) is None
        # Do not call a historically legal unclaimed import corrupt or terminal.
        with pytest.raises(queue.PendingResponse) as pending:
            read_visual_evidence([deepcopy(original)], cfg, invoke(cfg), execution=ex)
        job = read_json(folder/f'{pending.value.job_id}.job.json')
        assert job['prompt'].startswith('核对附带')
        assert not ex.snapshot()['completed']
    assert len(set(folder.glob('*.job.json'))-before) == 1


def test_missing_retry_claim_remains_integrity_failure(tmp_path):
    from daily_agent.terminal_fidelity import _receipt
    from daily_agent.batch_execution import digest as execution_digest
    cfg, original = strict_fixture(tmp_path); clock = StageClock()
    record = finish(cfg, original, clock, OfflineEvidence(clock, fail_pages=(1,)))
    receipt = record.reading['blocked']['pages'][0]['attempts'][1]
    folder = queue._folder(tmp_path)
    old_id = receipt['review_job_id']
    job = read_json(folder/f'{old_id}.job.json')
    job['retry'] = {'base_job_id': old_id, 'generation': 1}
    job_id = queue.digest(queue._contract(job))
    job.update(job_id=job_id, input_sha256=job_id)
    response = read_json(folder/f'{old_id}.answer.json')
    response.update(job_id=job_id, input_sha256=job_id)
    atomic_json(folder/f'{job_id}.job.json', job)
    atomic_json(folder/f'{job_id}.answer.json', response)
    atomic_json(folder/f'{old_id}.active.json', {'base_job_id': old_id, 'job_id': job_id})
    ex = SimpleNamespace(existing_operation=lambda identity: {
        'queue_job_id': job_id, 'input_sha256': execution_digest(queue._contract(job)),
        'queue_role': 'review', 'retry_generation': None})
    with pytest.raises(StateCorrupt, match='claim provenance'):
        _receipt(cfg, ex, (original.key, 'fidelity', 'review:1', 1), 'review')
