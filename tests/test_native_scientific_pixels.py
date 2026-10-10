"""Native science uses original pixels in the existing two bounded queue slots.

All model responses are offline fixtures imported into a real local parent queue.
These test transport, coverage and provenance gates, not actual model quality.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from daily_agent.batch_execution import Execution, candidate
from daily_agent.models import ApprovedItem
from daily_agent.paper_document import extract_document
from daily_agent.parent_writer import PendingResponse, import_response, pending
from daily_agent.reading import compact
from daily_agent.scientific_analysis import (
    FACETS, analyze_papers, reviewed_analysis, scientific_analysis_valid, validate_analysis,
)
from daily_agent.source_evidence_policy import (
    NATIVE, bind_source_evidence, native_quality, native_text_complete, verify_draft,
)
from test_native_claim_pixel_review import example, approve
from test_stage_execution import StageClock


@pytest.fixture
def native_science(example):
    """Complete actual PDF extraction + all chunks, then actual CORE queue review."""
    record, draft, config = example
    import fitz
    path = Path(record.paper_document['source_pdf_path'])
    with fitz.open(path) as pdf:
        page = pdf[0]
        page.insert_text((20, 16), 'Benchmark policies', fontsize=10)
        page.insert_text((20, 50), 'Methods', fontsize=10)
        page.insert_text((20, 85), 'Results', fontsize=10)
        page.insert_text((20, 162), 'Discussion', fontsize=10)
        pdf.saveIncr()
    doc = extract_document(path.read_bytes(), record, record.paper_document['source_url'],
                           {'min_body_chars': 100, 'ocr_enabled': False})
    doc['source_pdf_path'] = str(path)
    record.paper_document = doc
    assert doc['document_kind'] == 'full_text'
    chunks = doc['chunks']
    record.reading = {'complete': True, 'coverage': 1.0, 'total_chunks': len(chunks),
                      'fingerprint': 'offline-complete-native-reading',
                      'read_chunk_ids': [c['id'] for c in chunks],
                      'notes': [{'chunk_id': c['id'], 'summary': '离线测试逐块读取原文，区分模拟条件与结果。',
                                 'quotes': [c['text']], 'conditions': 'Simulation only', 'evidence_kind': 'simulation'}
                                for c in chunks]}
    for ref in draft.claim_evidence:
        chunk = next(c for c in chunks if compact(ref['quote']) in compact(c['text']))
        ref['chunk_id'] = chunk['id']
        ref['quote'] = next(line for line in chunk['text'].splitlines() if compact(ref['quote']) in compact(line))
    config.sources['reading'].update(source_evidence_policy=NATIVE, run_budget_seconds=30,
        visual_budget_seconds=20, fidelity_budget_seconds=40, max_chunks_per_paper=20)
    config.sources['llm_writer'].update(run_budget_seconds=60)
    bind_source_evidence(record, config)
    assert native_text_complete(record)
    verify_draft(draft, record)
    approve(example)
    assert native_quality(record, draft)
    fields = [('insight', ['valuable_insight', 'precise_problem']),
              ('explanation', ['bottleneck', 'minimal_idea', 'complexity']),
              ('argument', ['narrative', 'decisive_evidence', 'unresolved'])]
    refs = deepcopy(draft.claim_evidence)
    analysis = {'schema_version': 1, 'paragraphs': [
        {'id': section, 'claims': [{'id': section, 'kind': 'interpretation',
            'text': '模拟比较提供了策略更新的受限证据，尚不能单独定位机制。', 'facets': tags,
            'evidence': [{k: v for k, v in ref.items() if k != 'field'} for ref in refs]}]}
        for section, tags in fields]}
    assert validate_analysis(analysis, record)
    return record, draft, config, analysis


def review_response(job):
    payload = json.loads(job['prompt'].split('INPUT:\n', 1)[1])
    return {'input_sha256': payload['input_sha256'],
            'pixel_evidence_sha256': payload['pixel_evidence_sha256'],
            'checks': [{**row, 'supported': True, 'pixels_checked': True,
                        'reason': 'Offline fixture: exact scientific sentence is supported by the supplied original pixels.'}
                       for row in payload['claim_bindings']],
            'facets': [{'facet': facet, 'adequate': True, 'reason': 'Offline fixture: adequately qualified scientific analysis.'}
                       for facet in sorted(FACETS)]}


def queue_science_review(value, execution_factory=None):
    record, draft, config, analysis = value
    def run():
        if execution_factory:
            with execution_factory() as execution:
                analyze_papers(config, [record], [draft], execution=execution)
        else:
            analyze_papers(config, [record], [draft])
    with pytest.raises(PendingResponse): run()
    job = pending(config.root)[0]
    assert job['stage'] == 'draft' and job['images'] == []
    import_response(config.root, job['job_id'], analysis, 'scientific-author')
    with pytest.raises(PendingResponse): run()
    job = pending(config.root)[0]
    assert job['stage'] == 'review'
    return job, run


def complete_science(value):
    job, run = queue_science_review(value)
    import_response(value[2].root, job['job_id'], review_response(job), 'scientific-reviewer')
    run()
    assert scientific_analysis_valid(value[0], value[1])
    return job


def as_item(record, draft):
    record.reading['claim_evidence'] = deepcopy(draft.claim_evidence)
    record.reading['verification'] = deepcopy(draft.verification)
    return ApprovedItem(record.key, 'paper', record.title, record.source, record.url, draft.draft_fields, record)


def test_native_science_reads_all_text_and_reviews_each_claim_against_original_pixels(native_science):
    record, draft, config, analysis = native_science
    baseline = deepcopy(record.reading['notes'])
    job = complete_science(native_science)
    payload = json.loads(job['prompt'].split('INPUT:\n', 1)[1])
    assert len(job['images']) == 3  # exact source page + two original crops
    assert {i['sha256'] for i in job['images']} == set(payload['claim_bindings'][0]['image_sha256s'])
    assert len(payload['claim_bindings']) == 3
    assert '亲自查看' in job['prompt'] and '不做全页OCR' in job['prompt']
    assert record.reading['notes'] == baseline
    assert not record.reading.get('visual', {}).get('strict_fidelity')
    assert reviewed_analysis(as_item(record, draft)) == analysis
    before = sorted(p.name for p in (config.root / 'data/writer-queue').glob('*.job.json'))
    analyze_papers(config, [record], [draft])
    assert scientific_analysis_valid(record, draft)
    assert sorted(p.name for p in (config.root / 'data/writer-queue').glob('*.job.json')) == before


@pytest.mark.parametrize('mutation', ['new_number', 'field', 'claim', 'condition', 'crop', 'page', 'missing_page',
                                      'source', 'manifest', 'receipt', 'queue_response', 'protocol'])
def test_native_science_sink_rejects_tampering(native_science, mutation, monkeypatch):
    record, draft, config, _ = native_science
    job = complete_science(native_science)
    value = draft.draft_fields['scientific_analysis']
    stored = record.reading['scientific_analysis']
    claim = value['paragraphs'][0]['claims'][0]
    if mutation == 'new_number': claim['text'] += '提升99%。'
    elif mutation == 'field': draft.draft_fields['key_result'] += ' Guaranteed on hardware.'
    elif mutation == 'claim': claim['text'] += '这证明唯一因果机制。'
    elif mutation == 'condition': claim['evidence'][0]['conditions'] = 'Hardware validation'
    elif mutation == 'crop': Path(record.reading['paper_visual_assets']['assets'][0]['path']).write_bytes(b'tamper')
    elif mutation in {'page', 'missing_page'}:
        path = config.root / stored['native_pixel_review']['pixel_evidence']['page_evidence']['pages'][0]['path']
        path.unlink() if mutation == 'missing_page' else path.write_bytes(b'tamper')
    elif mutation == 'source': Path(record.paper_document['source_pdf_path']).write_bytes(b'%PDF-tamper')
    elif mutation == 'manifest': stored['native_pixel_review']['claim_bindings'][0]['image_sha256s'] = []
    elif mutation == 'receipt': stored['native_pixel_review']['review_receipt']['worker_id'] = 'scientific-author'
    elif mutation == 'protocol':
        import daily_agent.scientific_analysis as science
        monkeypatch.setattr(science, 'WRITER_PROMPT', science.WRITER_PROMPT + 'changed semantic policy')
    else:
        path = config.root / 'data/writer-queue' / f"{job['job_id']}.answer.json"
        answer = json.loads(path.read_text())
        answer['response']['checks'][0]['reason'] += 'tamper'
        path.write_text(json.dumps(answer))
    assert not scientific_analysis_valid(record, draft)
    assert reviewed_analysis(as_item(record, draft)) is None


@pytest.mark.parametrize('mutation', ['missing', 'unknown_image', 'stale_claim', 'false_pixels', 'unknown_pixels', 'bad_manifest', 'unsupported'])
def test_native_science_requires_every_claim_and_exact_pixels(native_science, mutation):
    record, draft, config, _ = native_science
    job, run = queue_science_review(native_science)
    response = review_response(job)
    row = response['checks'][0]
    if mutation == 'missing': response['checks'].pop()
    elif mutation == 'unknown_image': row['image_sha256s'] = ['0' * 64]
    elif mutation == 'stale_claim': row['claim_sha256'] = '0' * 64
    elif mutation == 'false_pixels': row['pixels_checked'] = False
    elif mutation == 'unknown_pixels': row['pixels_checked'] = None
    elif mutation == 'bad_manifest': response['pixel_evidence_sha256'] = '0' * 64
    else: row['supported'] = False
    import_response(config.root, job['job_id'], response, 'scientific-reviewer')
    run()
    assert not scientific_analysis_valid(record, draft)
    assert 'scientific_analysis' not in draft.draft_fields
    assert native_quality(record, draft)  # failed addition never erases reviewed CORE
    assert record.reading['scientific_analysis']['status'] == 'failed'
    before = len(list((config.root / 'data/writer-queue').glob('*.job.json')))
    run()
    assert len(list((config.root / 'data/writer-queue').glob('*.job.json'))) == before


@pytest.mark.parametrize('mutation', ['incomplete', 'unreviewed_core', 'strict_without_fidelity', 'no_model', 'unknown_policy', 'default_strict'])
def test_science_never_bypasses_required_reading_or_core(native_science, mutation):
    record, draft, config, _ = native_science
    if mutation == 'incomplete': record.reading['read_chunk_ids'].pop()
    elif mutation == 'unreviewed_core': draft.verification['native_claim_review']['status'] = 'not_reviewed'
    elif mutation == 'strict_without_fidelity': record.reading.pop('source_evidence')
    elif mutation == 'unknown_policy': record.reading['source_evidence']['policy'] = 'unknown'
    elif mutation == 'default_strict': config.sources['reading'].pop('source_evidence_policy')
    analyze_papers(config, [record], [draft], use_llm=mutation != 'no_model')
    assert not pending(config.root)
    assert 'scientific_analysis' not in draft.draft_fields


def test_science_native_two_existing_slots_resume_without_budget_reset(native_science):
    record, draft, config, _ = native_science
    clock = StageClock()
    frozen = [candidate(record)]
    def execution():
        return Execution(config.root / 'issue', 'native-science-offline', frozen, config,
                         protocol='native-science-fixture', clock=clock)
    job, run = queue_science_review(native_science, execution)
    for _ in range(3):
        with pytest.raises(PendingResponse): run()
        assert pending(config.root)[0]['job_id'] == job['job_id']
    import_response(config.root, job['job_id'], review_response(job), 'scientific-reviewer')
    run()
    with execution() as active:
        state = active.snapshot()
        assert {tuple(op['identity']) for op in state['operations'].values()} == {
            (record.key, 'primary_writer', 'scientific_writer', 0),
            (record.key, 'primary_writer', 'scientific_review', 0)}
        assert state['pools']['primary_writer']['limit'] == 60
        assert state['writer_circuit'] is None
    assert scientific_analysis_valid(record, draft)


def test_exhausted_science_budget_creates_no_queue_work(native_science):
    record, draft, config, _ = native_science
    config.sources['llm_writer']['run_budget_seconds'] = 0
    with Execution(config.root / 'issue', 'zero-budget', [candidate(record)], config,
                   protocol='native-science-fixture') as execution:
        analyze_papers(config, [record], [draft], execution=execution)
        assert execution.snapshot()['operations'] == {}
    assert not pending(config.root)
    assert record.reading['scientific_analysis']['status'] == 'not_reviewed'


def test_science_cancellation_propagates_without_review_or_later_work(native_science):
    record, draft, config, _ = native_science
    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt('offline cancellation')
    with Execution(config.root / 'issue', 'cancelled', [candidate(record)], config,
                   protocol='native-science-fixture') as execution:
        with pytest.raises(KeyboardInterrupt):
            analyze_papers(config, [record], [draft], execution=execution, invoke=cancelled)
        assert execution.snapshot()['operations'] == {}
        assert execution.snapshot()['reservations'] == {}
    assert not pending(config.root)
    assert 'scientific_analysis' not in draft.draft_fields


def test_native_science_fake_callback_cannot_authorize_pixels(native_science):
    record, draft, config, analysis = native_science
    called = []
    def fabricated(prompt, timeout, **kwargs):
        called.append(kwargs['stage'])
        return deepcopy(analysis)
    analyze_papers(config, [record], [draft], invoke=fabricated)
    assert called == ['draft']
    assert not scientific_analysis_valid(record, draft)
    assert 'scientific_analysis' not in draft.draft_fields
    assert not pending(config.root)


def test_native_science_author_cannot_import_independent_pixel_review(native_science):
    job, _ = queue_science_review(native_science)
    with pytest.raises(ValueError, match='different worker'):
        import_response(native_science[2].root, job['job_id'], review_response(job), 'scientific-author')
    assert 'scientific_analysis' not in native_science[1].draft_fields


def test_new_number_in_writer_is_rejected_before_pixel_review(native_science):
    record, draft, config, analysis = native_science
    analysis['paragraphs'][0]['claims'][0]['text'] += '提升99%。'
    with pytest.raises(PendingResponse):
        analyze_papers(config, [record], [draft])
    job = pending(config.root)[0]
    import_response(config.root, job['job_id'], analysis, 'scientific-author')
    with pytest.raises(PendingResponse):
        analyze_papers(config, [record], [draft])
    repair = pending(config.root)[0]
    assert repair['stage'] == 'draft' and repair['job_id'] != job['job_id']
    assert 'missing_numeric_tokens' in repair['prompt']
    import_response(config.root, repair['job_id'], analysis, 'scientific-author')
    analyze_papers(config, [record], [draft])
    assert not pending(config.root)
    assert record.reading['scientific_analysis']['status'] == 'failed'
    assert 'scientific_analysis' not in draft.draft_fields


def test_science_reviews_cited_pages_without_expanding_to_every_pdf_page(example):
    record, _, config = example
    import fitz
    path = Path(record.paper_document['source_pdf_path'])
    with fitz.open(path) as pdf:
        for number in range(9):
            page = pdf.new_page(width=380, height=260)
            page.insert_textbox(fitz.Rect(20, 20, 360, 240),
                ('Additional native prose discusses simulation conditions and bounded comparisons.\n' * 3),
                fontsize=10)
        pdf.saveIncr()
    value = native_science.__wrapped__(example)
    assert value[0].paper_document['source_page_count'] == 10
    assert len(value[0].reading['read_chunk_ids']) == len(value[0].paper_document['chunks'])
    job = complete_science(value)
    assert len(job['images']) == 3
    evidence = value[0].reading['scientific_analysis']['native_pixel_review']['pixel_evidence']
    assert [page['page'] for page in evidence['page_evidence']['pages']] == [1]
    assert len(pending(config.root)) == 0
