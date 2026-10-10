"""Offline evidence reuse, negative provenance cases, and exact queue recovery."""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_agent import parent_writer, visual_fidelity, visual_reading
from daily_agent.batch_execution import ExecutionConflict
from daily_agent.paper_document import digest
from daily_agent.paper_visual_assets import _candidate_pages
from daily_agent.visual_inventory import (read_visual_evidence, derive_inventory,
    verified_inventory, inventory_assets, writer_visual_observations, BASIS)
from daily_agent.workflow_state import read_json, atomic_json, StateCorrupt
from test_fidelity_first import fixture, ledger, StageClock


def asset(kind, n=1):
    contents = {
        'figure': {'axes': ['iteration', 'success rate (%)'], 'legend': ['fixed baseline'],
                   'observations': ['Labelled simulation ablation curve']},
        'formula': {'latex': r'x_{i}^{2} = y_i'},
        'table': {'columns': ['label', 'value'], 'rows': [['fixed baseline', '81.2']]}}
    return {'id': kind+str(n), 'kind': kind, 'bbox': [.1, .1, .8, .8], 'content': contents[kind]}


class OfflineEvidence:
    def __init__(self, clock, fail_pages=(), per_page=None):
        self.clock, self.fail_pages, self.per_page = clock, fail_pages, per_page or {}
        self.calls = []

    def __call__(self, prompt, timeout, image=None, **kwargs):
        value = json.loads(prompt.split('输入：\n', 1)[1])
        page = value['page']
        if prompt.startswith('你是论文页面转写员'):
            phase = 'transcribe'
            assets = self.per_page.get(page, [asset('figure'), asset('formula'), asset('table')])
            result = {'page': page, 'text': value['native_text'], 'unresolved': [], 'assets': assets}
        elif prompt.startswith('你是独立的页面保真复核员'):
            phase = 'review'
            result = {'page': page, 'text_supported': page not in self.fail_pages,
                      'inventory_complete': page not in self.fail_pages,
                      'checks': [{'id': a['id'], 'supported': page not in self.fail_pages,
                                  'reason': 'offline independent pixel fixture'} for a in value['assets']],
                      'issues': ['missing asset'] if page in self.fail_pages else []}
        elif prompt.startswith('核对附带'):
            phase = 'visual'
            result = {'page': page, 'summary': '旧原生抽取核对记录', 'figures': ['实验曲线'],
                      'tables': [], 'formulas': [], 'text_matches_image': True, 'issues': []}
        else:
            raise AssertionError('Unexpected prompt')
        self.calls.append(phase)
        self.clock.advance(1)
        return result


def run(cfg, original, clock, invoke):
    record = deepcopy(original)
    with ledger(cfg, original, clock) as execution:
        read_visual_evidence([record], cfg, invoke, execution=execution)
        state = execution.snapshot()
    return record, state


@pytest.mark.parametrize('count', [2, 8, 16])
def test_cold_full_success_saves_one_visual_call_per_page_and_keeps_reviews(tmp_path, count):
    cfg, original = fixture(tmp_path, count)
    clock = StageClock(); fake = OfflineEvidence(clock)
    record, state = run(cfg, original, clock, fake)
    assert Counter(fake.calls) == {'transcribe': count, 'review': count}
    visual = record.reading['visual']; inventory = verified_inventory(visual)
    assert inventory['basis'] == BASIS and len(inventory['pages']) == count
    assert 'notes' not in visual and 'passed' not in visual and 'text_matches_image' not in json.dumps(inventory)
    assert all(len(p['asset_refs']) == 3 for p in inventory['pages'])
    assert all('content' not in ref for p in inventory['pages'] for ref in p['asset_refs'])
    assert all(inventory_assets(visual, p) == record.reading['visual']['fidelity']['pages'][i]['candidate']['assets']
               for i, p in enumerate(inventory['pages']))
    assert state['pools']['visual']['remaining'] == state['pools']['visual']['limit'] == 100
    assert state['pools']['visual']['charged'] == 0
    assert all(p['limit'] == 100 for k, p in state['pools'].items() if k in {'native', 'repaired', 'visual', 'fidelity'})
    # Only the unchanged fidelity protocol wrote acceptance artifacts.
    assert not list((tmp_path/'data/reading/visual').glob('[0-9a-f]'*64+'.json'))
    from daily_agent.reading import audit_reading
    assert audit_reading([record.to_dict()])['quality_passed_count'] == 0


def test_complete_cache_replay_and_old_visual_evidence_are_preserved(tmp_path):
    cfg, original = fixture(tmp_path)
    clock = StageClock(); fake = OfflineEvidence(clock)
    legacy = deepcopy(original)
    visual_reading.read_visuals([legacy], cfg, fake)
    old_visual = deepcopy(legacy.reading['visual'])
    visual_fidelity.repair_visuals([legacy], cfg, fake)
    fake.calls.clear()
    record, _ = run(cfg, original, clock, fake)
    assert fake.calls == []
    for key, value in old_visual.items():
        if key != 'strict_fidelity':
            assert record.reading['visual'][key] == value
    assert verified_inventory(record.reading['visual'])
    fake.calls.clear()
    run(cfg, original, clock, fake)
    assert fake.calls == []


@pytest.mark.parametrize('fail_pages', [(1,), (1, 2)])
def test_failed_or_partial_fidelity_retains_fallback_and_exact_failure_evidence(tmp_path, fail_pages):
    cfg, original = fixture(tmp_path)
    clock = StageClock(); fake = OfflineEvidence(clock, fail_pages)
    record, _ = run(cfg, original, clock, fake)
    visual = record.reading['visual']
    assert fake.calls.count('visual') == 2
    assert visual['passed'] and visual['complete']  # Native check never overrides strict failure.
    assert visual['strict_fidelity'] is False and visual['fidelity']['passed'] is False
    assert [p['page'] for p in visual['fidelity']['pages'] if not p['passed']] == list(fail_pages)
    assert not record.paper_text_status['sufficient_for_deep_summary']
    assert record.paper_document == original.paper_document
    assert 'asset_inventory' not in visual


def test_bad_image_hash_does_not_derive_inventory_or_fake_completion(tmp_path):
    cfg, original = fixture(tmp_path)
    original.paper_document['pages'][1]['image_hash'] = 'f'*64
    clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    visual = record.reading['visual']
    assert not visual['strict_fidelity'] and not visual['complete']
    assert visual['fidelity']['pages'][1]['reason'] == 'ValueError'
    assert not verified_inventory(visual)
    assert not record.paper_text_status['sufficient_for_deep_summary']


def test_nonrequired_page_blocks_whole_document_inventory(tmp_path):
    cfg, original = fixture(tmp_path)
    original.paper_document['pages'][1]['visual_required'] = False
    clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    assert not verified_inventory(record.reading['visual'])
    assert record.reading['visual']['fidelity']['passed'] is False
    assert fake.calls.count('visual') == 1


@pytest.mark.parametrize('field', ['candidate', 'review', 'image_hash', 'native_text_hash', 'fingerprint'])
def test_derivation_rechecks_exact_accepted_candidate_review_and_source(tmp_path, field):
    cfg, original = fixture(tmp_path)
    clock = StageClock(); record, _ = run(cfg, original, clock, OfflineEvidence(clock))
    value = record.reading['visual']['fidelity']['pages'][0]
    if field == 'candidate': value[field]['assets'][0]['content']['observations'] = ['fabricated result']
    elif field == 'review': value[field]['checks'][0]['supported'] = False
    else: value[field] = 'a'*64
    record.paper_document['fidelity_manifest_hash'] = digest(record.reading['visual']['fidelity'])
    assert derive_inventory(record, cfg) is None
    assert verified_inventory(record.reading['visual']) is None


def test_empty_page_inventory_is_retained_but_not_nominated(tmp_path):
    cfg, original = fixture(tmp_path)
    clock = StageClock(); fake = OfflineEvidence(clock, per_page={1: []})
    record, _ = run(cfg, original, clock, fake)
    inv = verified_inventory(record.reading['visual'])
    assert len(inv['pages']) == 2 and inv['pages'][0]['asset_refs'] == []
    assert _candidate_pages(record.reading['visual']) == [2]


def test_plot_priority_preserves_formula_table_and_original_asset_content(tmp_path):
    cfg, original = fixture(tmp_path, 16)
    content = {n: [{'id': 'framework', 'kind': 'figure', 'bbox': [.1, .1, .9, .9],
                    'content': {'axes': [], 'legend': [], 'observations': ['Architecture nodes and arrows']}}]
               for n in range(1, 15)}
    content[2] = [asset('formula')]; content[3] = [asset('table')]; content[15] = [asset('figure')]; content[16] = []
    clock = StageClock(); record, _ = run(cfg, original, clock, OfflineEvidence(clock, per_page=content))
    selected = _candidate_pages(record.reading['visual'])
    assert len(selected) == 8 and selected[0] == 15 and {2, 3} <= set(selected)
    assert 16 not in selected
    visual = record.reading['visual']; inv = verified_inventory(visual)
    assert inventory_assets(visual, inv['pages'][2])[0]['content']['rows'] == [['fixed baseline', '81.2']]
    visual['asset_inventory']['pages'][0]['asset_refs'][0]['sha256'] = 'f'*64
    assert _candidate_pages(visual) == []


def queue_invoke(cfg):
    return lambda prompt, timeout, image=None, **kwargs: parent_writer.request(cfg.root, prompt, timeout, image, **kwargs)


def admit_original_visual(cfg, original, clock):
    cfg.sources['reading']['concurrent_reads'] = 1
    with ledger(cfg, original, clock) as execution:
        with pytest.raises(parent_writer.PendingResponse) as pending:
            visual_reading.read_visuals([deepcopy(original)], cfg, queue_invoke(cfg), execution=execution)
        return pending.value.job_id, execution.snapshot()


def answer_job(cfg, job_id, fake):
    job = read_json(cfg.root/'data/writer-queue'/f'{job_id}.job.json')
    response = fake(job['prompt'], 10)
    worker = 'independent-reviewer' if job['stage'] == 'review' else 'page-reader'
    claim = parent_writer.claim(cfg.root, job_id, worker)
    parent_writer.import_response(cfg.root, job_id, response, worker, claim_token=claim['token'])


def drain(cfg, original, clock, fake):
    for _ in range(20):
        try:
            return run(cfg, original, clock, queue_invoke(cfg))
        except parent_writer.PendingResponse as pending:
            answer_job(cfg, pending.job_id, fake)
    pytest.fail('Offline queue did not finish within finite page topology')


def test_bound_pending_visual_waits_before_cache_or_fidelity_shortcuts(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    job, before = admit_original_visual(cfg, original, clock)
    # Even complete caches must not hide an admitted, unanswered legacy request.
    cached = deepcopy(original)
    visual_reading.read_visuals([cached], cfg, fake)
    visual_fidelity.repair_visuals([cached], cfg, fake)
    with pytest.raises(parent_writer.PendingResponse) as pending:
        run(cfg, original, clock, queue_invoke(cfg))
    assert pending.value.job_id == job
    with ledger(cfg, original, clock) as execution:
        assert execution.snapshot()['operations'] == before['operations']
    answer_job(cfg, job, fake)
    record, after = drain(cfg, original, clock, fake)
    for key, value in before['operations'].items(): assert after['operations'][key] == value
    assert verified_inventory(record.reading['visual'])


def test_bound_answer_is_processed_without_admitting_unused_visual_pages(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    job, before = admit_original_visual(cfg, original, clock)
    answer_job(cfg, job, fake)
    record, after = drain(cfg, original, clock, fake)
    assert len([o for o in after['operations'].values() if o['identity'][1] == 'visual']) == 1
    for key, value in before['operations'].items(): assert after['operations'][key] == value
    assert verified_inventory(record.reading['visual'])
    assert record.paper_document['native_document'] == original.paper_document


@pytest.mark.parametrize('fault', ['retired', 'wrong_owner', 'invalid_received', 'missing_job'])
def test_bound_retired_or_tampered_jobs_fail_closed_without_rebinding(tmp_path, fault):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    job, before = admit_original_visual(cfg, original, clock)
    answer_job(cfg, job, fake)
    q = cfg.root/'data/writer-queue'
    if fault == 'retired': atomic_json(q/f'{job}.active.json', {'base_job_id': job, 'job_id': 'f'*64})
    elif fault == 'missing_job': (q/f'{job}.job.json').unlink()
    elif fault == 'wrong_owner':
        path = q/f'{job}.claim.json'; data = read_json(path); data['worker_id'] = 'forged'; atomic_json(path, data)
    else:
        path = q/f'{job}.answer.json'; data = read_json(path)
        data['received_at'] = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(); atomic_json(path, data)
    with pytest.raises((ExecutionConflict, StateCorrupt)):
        run(cfg, original, clock, queue_invoke(cfg))
    with ledger(cfg, original, clock) as execution:
        assert execution.snapshot()['operations'] == before['operations']


def test_bound_valid_old_answer_survives_later_claim_expiry(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    job, _ = admit_original_visual(cfg, original, clock); answer_job(cfg, job, fake)
    # Move the entire historical job/answer/lease window into the past. Current
    # expiry does not change the original queue contract or validity at import.
    q = cfg.root/'data/writer-queue'; now = datetime.now(timezone.utc)
    for suffix, fields in [('job', ['created_at', 'expires_at']), ('answer', ['received_at']), ('claim', ['expires_at'])]:
        path = q/f'{job}.{suffix}.json'; value = read_json(path)
        for field in fields: value[field] = (datetime.fromisoformat(value[field])-timedelta(days=1)).isoformat()
        atomic_json(path, value)
    # The unchanged lower reader will consume the old answer; cache loading has
    # no expiration rule. Skip new transport by retaining its exact note cache.
    visual_reading.read_visuals([deepcopy(original)], cfg, fake)
    record, _ = run(cfg, original, clock, fake)
    assert verified_inventory(record.reading['visual'])


def test_writer_metadata_is_bounded_and_has_no_duplicate_asset_content(tmp_path, capsys):
    cfg, original = fixture(tmp_path, 16); clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    visual = record.reading['visual']; compact = writer_visual_observations(visual)
    naive = {k: v for k, v in visual.items() if k != 'fidelity'}
    compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode())
    naive_bytes = len(json.dumps(naive, ensure_ascii=False).encode())
    old = deepcopy(original)
    visual_reading.read_visuals([old], cfg, fake)
    old_bytes = len(json.dumps(old.reading['visual'], ensure_ascii=False).encode())
    assert compact_bytes < 1000 and compact_bytes < naive_bytes
    assert compact['required_pages'] == 16 and compact['strict_fidelity'] is True
    assert compact['asset_inventory']['basis'] == BASIS
    assert compact['asset_inventory']['fidelity_manifest_sha256'] == digest(visual['fidelity'])
    assert '81.2' not in json.dumps(compact) and 'latex' not in json.dumps(compact)
    print(json.dumps({'pages': 16, 'compact_writer_visual_bytes': compact_bytes,
                      'unslimmed_inventory_bytes': naive_bytes, 'old_native_visual_bytes': old_bytes}, sort_keys=True))


def test_renderer_labels_inventory_separately_and_keeps_original_contents(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    from daily_agent.rendering.notes import reading_label, write_reading_notes
    assert '已复核资产目录 2/2 页' in reading_label(record)
    cfg.reports_dir = tmp_path/'reports'
    cfg.sources['report_writing'] = {'source_screenshots_enabled': False}
    item = SimpleNamespace(item_type='paper', material=record, key=record.key, title=record.title,
                           final_fields={'confidence': 'low'})
    write_reading_notes(cfg, [item], date(2026, 10, 9))
    markdown = next(cfg.reports_dir.rglob('*.md')).read_text()
    assert '不是原生文本与图片匹配证书' in markdown
    assert '81.2' in markdown and 'x_{i}^{2}' in markdown and 'ablation curve' in markdown
    assert 'text_matches_image' not in markdown


@pytest.mark.parametrize('count, page_numbers', [(3, [1, 2]), (2, [1, 3]), (None, [1, 2])])
def test_partial_pdf_source_retains_fallback_even_if_page_reviews_all_accept(tmp_path, count, page_numbers):
    cfg, original = fixture(tmp_path)
    original.paper_document.update(source_type='pdf', source_page_count=count, source_pdf_sha256='a'*64,
                                   document_kind='partial_text')
    for page, number in zip(original.paper_document['pages'], page_numbers): page['page'] = number
    clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    visual = record.reading['visual']
    assert visual['fidelity']['passed'] is True  # Preserve exactly what lower stage found.
    assert visual['strict_fidelity'] is False
    assert visual['inventory_failure']['reason'] == 'source_page_coverage_incomplete'
    assert 'asset_inventory' not in visual and fake.calls.count('visual') == 2
    assert not record.paper_text_status['sufficient_for_deep_summary']


@pytest.mark.parametrize('phase', ['transcribe', 'review', 'visual'])
def test_real_workflow_cancel_is_not_swallowed_or_followed_by_new_calls(tmp_path, phase):
    from daily_agent.workflow_runtime import WorkflowCancelled
    cfg, original = fixture(tmp_path, 8)
    cfg.sources['reading']['concurrent_reads'] = 1
    clock = StageClock(); fake = OfflineEvidence(clock, fail_pages=tuple(range(1, 9)) if phase == 'visual' else ())
    calls = []
    cancellation = WorkflowCancelled('stop actual workflow')
    def cancelled(prompt, *args, **kwargs):
        actual = 'transcribe' if prompt.startswith('你是论文页面转写员') else 'review' if prompt.startswith('你是独立') else 'visual'
        calls.append(actual)
        if actual == phase: raise cancellation
        return fake(prompt, *args, **kwargs)
    with pytest.raises(WorkflowCancelled) as error:
        run(cfg, original, clock, cancelled)
    assert error.value is cancellation
    assert calls[-1] == phase and calls.count(phase) == 1
    if phase != 'visual': assert 'visual' not in calls


@pytest.mark.parametrize('count', [2, 8, 16])
def test_exact_cold_baseline_call_delta_and_equal_reviewed_asset_content(tmp_path, count):
    cfg_old, old = fixture(tmp_path/'old', count)
    cfg, original = fixture(tmp_path/'new', count)
    clock = StageClock(); legacy = OfflineEvidence(clock); new = OfflineEvidence(clock)
    visual_reading.read_visuals([old], cfg_old, legacy)
    visual_fidelity.repair_visuals([old], cfg_old, legacy)
    record, _ = run(cfg, original, clock, new)
    assert len(legacy.calls)-len(new.calls) == count
    assert legacy.calls.count('review') == new.calls.count('review') == count
    assert legacy.calls.count('transcribe') == new.calls.count('transcribe') == count
    assert [r['candidate'] for r in old.reading['visual']['fidelity']['pages']] == [r['candidate'] for r in record.reading['visual']['fidelity']['pages']]
    print(json.dumps({'pages': count, 'baseline': dict(Counter(legacy.calls)), 'reuse': dict(Counter(new.calls)),
                      'saved_fake_model_jobs': count}, sort_keys=True))


def test_complete_mismatch_cache_is_kept_without_forging_native_match(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    def mismatch(prompt, *args, **kwargs):
        note = fake(prompt, *args, **kwargs)
        note.update(text_matches_image=False, issues=['native superscript missing'])
        return note
    legacy = deepcopy(original)
    visual_reading.read_visuals([legacy], cfg, mismatch)
    previous = deepcopy(legacy.reading['visual'])
    record, _ = run(cfg, original, clock, fake)
    assert record.reading['visual']['notes'] == previous['notes']
    assert record.reading['visual']['passed'] is False
    assert record.reading['visual']['mismatch_pages'] == [1, 2]
    assert record.reading['visual']['strict_fidelity'] is True


def test_inventory_candidates_are_sent_to_pixel_selector_with_original_content(tmp_path, monkeypatch):
    import fitz
    from daily_agent.paper_document import build_document, attach_document
    from daily_agent.paper_visual_assets import request_visual_selection
    cfg, original = fixture(tmp_path)
    pdf_path = tmp_path/'paper.pdf'; pages = []
    with fitz.open() as pdf:
        for n in range(2):
            page = pdf.new_page(width=250, height=220)
            page.insert_text((25, 40), 'Table 1. Success rate (%) 81.2')
        pdf.save(pdf_path)
    with fitz.open(pdf_path) as pdf:
        for n, native in enumerate(original.paper_document['pages'], 1):
            image = tmp_path/f'original-{n}.png'; pdf[n-1].get_pixmap().save(image)
            pages.append({**native, 'image_path': str(image), 'image_hash': hashlib.sha256(image.read_bytes()).hexdigest()})
    doc = build_document(original, pages, original.url, 'pdf', cfg.sources['paper_text'])
    doc.update(source_pdf_path=str(pdf_path), source_pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(), source_page_count=2)
    attach_document(original, doc)
    clock = StageClock(); record, _ = run(cfg, original, clock, OfflineEvidence(clock))
    captured = []
    def selector(root, prompt, timeout, image_path, stage):
        captured.append((prompt, image_path, stage))
        return {'selection_policy_version': 2}
    monkeypatch.setattr(parent_writer, 'request', selector)
    request_visual_selection(record, tmp_path)
    prompt, images, stage = captured[0]
    assert len(images) == 2 and stage == 'visual_selection'
    data = json.loads(prompt[prompt.index('{"source_pdf_sha256"'):])
    assert all(page['notes'] == [] for page in data['pages'])
    assert data['pages'][0]['reviewed_asset_inventory']['basis'] == BASIS
    assert data['pages'][0]['reviewed_asset_inventory']['assets'][2]['content']['rows'] == [['fixed baseline', '81.2']]
    assert 'metadata is not a scientific claim' in prompt


def test_unvalidated_attached_visual_pass_is_not_reused_as_old_evidence(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    original.reading['visual'] = {'required_pages': 2, 'complete': True, 'passed': True, 'notes': [
        {'page': n, 'summary': 'unbacked old note', 'figures': ['fabricated'], 'tables': [], 'formulas': [],
         'text_matches_image': True, 'issues': []} for n in (1, 2)], 'failures': [], 'mismatch_pages': []}
    record, _ = run(cfg, original, clock, fake)
    assert 'notes' not in record.reading['visual'] and 'passed' not in record.reading['visual']
    assert verified_inventory(record.reading['visual'])


def test_source_contract_tamper_invalidates_inventory_consumption(tmp_path):
    cfg, original = fixture(tmp_path); clock = StageClock(); fake = OfflineEvidence(clock)
    record, _ = run(cfg, original, clock, fake)
    record.reading['visual']['asset_inventory']['source_contract']['native_document_sha256'] = 'f'*64
    assert verified_inventory(record.reading['visual']) is None
    assert _candidate_pages(record.reading['visual']) == []


def test_bound_visual_queue_lock_contention_suspends_with_actual_job(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from daily_agent.batch_dispatch import QueueObservationBusy
    cfg, original = fixture(tmp_path); clock = StageClock()
    job, before = admit_original_visual(cfg, original, clock)
    calls = []
    @contextmanager
    def busy(path, *, timeout, strict_io):
        calls.append((path, timeout, strict_io))
        raise TimeoutError('queue observation is contended')
        yield
    monkeypatch.setattr(parent_writer, 'queue_lock', busy)
    with pytest.raises(QueueObservationBusy) as pending:
        run(cfg, original, clock, queue_invoke(cfg))
    assert pending.value.job_id == job and pending.value.batch_id == 'batch'
    assert calls == [(cfg.root/'data/writer-queue/queue.lock', 1, True)]
    with ledger(cfg, original, clock) as execution:
        assert execution.snapshot()['operations'] == before['operations']


@pytest.mark.parametrize('error', [TimeoutError('body timeout'), StateCorrupt('body corruption')])
def test_bound_guard_body_errors_are_not_relabelled_as_queue_contention(tmp_path, monkeypatch, error):
    cfg, original = fixture(tmp_path); clock = StageClock()
    _, before = admit_original_visual(cfg, original, clock)
    def fail(*args, **kwargs): raise error
    monkeypatch.setattr(parent_writer, 'validate_job', fail)
    with pytest.raises(type(error)) as received:
        run(cfg, original, clock, queue_invoke(cfg))
    assert received.value is error
    with ledger(cfg, original, clock) as execution:
        assert execution.snapshot()['operations'] == before['operations']
