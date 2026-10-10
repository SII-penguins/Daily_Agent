"""Native policy integration gates, exact cache reuse and finite frozen slots.

All responses are deterministic offline fixtures; no real model or network work.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from daily_agent import reading
from daily_agent.batch_execution import Execution, ExecutionConflict, candidate
from daily_agent.cloud_workflow import _qualifying_rows
from daily_agent.deferred_review_cache import _supported, _core_cache_supported, _load, draft_report_items
from daily_agent.models import ApprovedItem
from daily_agent.paper_document import extract_document, digest
from daily_agent.reading_batches import fingerprints
from daily_agent.source_evidence_policy import (
    STRICT, NATIVE, configured_policy, expected_record_policy, record_policy,
    native_reading_config, bind_source_evidence, native_text_complete,
    native_quality, verify_draft, audit_reading, source_text_gaps,
)
from test_native_claim_pixel_review import example, approve


@pytest.fixture
def native_supported(example):
    record, draft, config = example
    import fitz
    path = Path(record.paper_document['source_pdf_path'])
    with fitz.open(path) as pdf:
        page = pdf[0]
        for y, text in [(16, 'Benchmark policies'), (50, 'Methods'), (85, 'Results'), (162, 'Discussion')]:
            page.insert_text((20, y), text, fontsize=10)
        pdf.saveIncr()
    record.paper_document = extract_document(path.read_bytes(), record, 'https://example.org/paper.pdf',
                                             {'min_body_chars': 100})
    record.paper_document['source_pdf_path'] = str(path)
    config.sources['reading'].update(source_evidence_policy=NATIVE, run_budget_seconds=30,
        visual_budget_seconds=20, fidelity_budget_seconds=40, max_chunks_per_paper=20)
    config.sources['llm_writer'].update(run_budget_seconds=60)
    def read(prompt, timeout):
        chunk = json.loads(prompt.split('输入：\n', 1)[1])
        return {'chunk_id': chunk['id'], 'summary': '离线夹具逐块阅读原文，保留原始引句。',
                'quotes': [chunk['text'].strip()], 'conditions': 'Simulation only', 'evidence_kind': 'simulation'}
    reading.read_papers([record], native_reading_config(config), read)
    for ref in draft.claim_evidence:
        chunk = next(c for c in record.paper_document['chunks'] if reading.compact(ref['quote']) in reading.compact(c['text']))
        ref['chunk_id'] = chunk['id']
        ref['quote'] = next(line for line in chunk['text'].splitlines() if reading.compact(ref['quote']) in reading.compact(line))
    bind_source_evidence(record, config)
    verify_draft(draft, record)
    approve(example)
    record.detail = deepcopy(draft.draft_fields)
    record.reading['claim_evidence'] = deepcopy(draft.claim_evidence)
    record.reading['verification'] = deepcopy(draft.verification)
    assert native_quality(record, draft)
    return record, draft, config


def row(record, draft):
    return ApprovedItem(record.key, 'paper', record.title, record.source, record.url,
                        deepcopy(draft.draft_fields), deepcopy(record)).to_dict()


def test_policy_default_unknown_and_non_pdf_compatibility(example):
    record, _, config = example
    assert configured_policy(config) == STRICT
    config.sources['reading']['source_evidence_policy'] = 'invented_mode'
    with pytest.raises(ValueError): configured_policy(config)
    config.sources['reading']['source_evidence_policy'] = NATIVE
    assert expected_record_policy(config, record) == NATIVE
    record.paper_document['source_type'] = 'html'
    assert expected_record_policy(config, record) == STRICT


def test_exact_raw_note_protocol_identity_reused_without_quality_inflation(native_supported):
    record, _, config = native_supported
    raw = native_reading_config(config)
    baseline = deepcopy(config)
    baseline.sources['reading'].pop('source_evidence_policy')
    assert fingerprints(record.paper_document, raw) == fingerprints(record.paper_document, baseline)
    assert record.reading['fingerprint'] == fingerprints(record.paper_document, raw)[0]
    assert configured_policy(config) == NATIVE  # no mutation of frozen config
    assert record_policy(record) == NATIVE
    assert not record.reading.get('visual', {}).get('strict_fidelity')
    assert reading.audit_reading([record.to_dict()])['all_passed'] is False  # old strict gate unchanged
    current = audit_reading([record.to_dict()])
    assert current['all_passed'] is False
    assert current['papers'][0]['core_quality_passed'] is True
    assert current['papers'][0]['completion_scope'] == 'core_only'


def test_complete_native_does_not_claim_every_glyph_or_image_trustworthy(native_supported):
    record, _, config = native_supported
    record.paper_document['pages'][-1]['text'] += '\x00'
    record.paper_document['chunks'][-1]['text'] += '\x00'
    record.paper_document['content_hash'] = digest('\n'.join(p['text'] for p in record.paper_document['pages']))
    bind_source_evidence(record, config)
    assert source_text_gaps(record)[0]['kind'] == 'unreadable_text_glyphs'
    assert native_text_complete(record)  # all extracted chunks covered, gap explicit
    assert not audit_reading([record.to_dict()])['all_passed']  # old pixel receipt cannot cover changed source doc


@pytest.mark.parametrize('mutation', ['page_count', 'missing_page', 'chunk', 'note', 'partial', 'protocol', 'nested'])
def test_source_and_read_coverage_cannot_be_promoted_by_markers(native_supported, mutation):
    record, _, config = native_supported
    if mutation == 'page_count': record.paper_document['source_page_count'] += 1
    elif mutation == 'missing_page': record.paper_document['pages'].pop()
    elif mutation == 'chunk': record.paper_document['chunks'].pop()
    elif mutation == 'note': record.reading['notes'].pop()
    elif mutation == 'partial': record.paper_document['document_kind'] = 'partial_text'
    elif mutation == 'protocol': record.reading['source_evidence']['protocol_sha256'] = '0' * 64
    else: record.paper_document['native_document'] = {'source_type': 'pdf', 'source_pdf_sha256': '0' * 64}
    assert not native_text_complete(record)
    assert not audit_reading([record.to_dict()])['all_passed']


def test_final_published_fields_and_config_policy_are_rechecked(native_supported):
    record, draft, config = native_supported
    approved = row(record, draft)
    assert not _qualifying_rows([approved], config=config)[0]  # CORE alone is not complete daily selection
    assert _qualifying_rows([approved], config=config)[1][0]['reasons'] == ['required_scientific_analysis_incomplete']
    changed = deepcopy(approved)
    changed['final_fields']['key_result'] += ' All real robots are guaranteed to improve.'
    assert not _qualifying_rows([changed], config=config)[0]
    strict = deepcopy(config)
    strict.sources['reading'].pop('source_evidence_policy')
    assert not _supported(record, draft, config=strict)
    assert not _qualifying_rows([approved], config=strict)[0]


def save_native_cache(record, draft, config):
    def original(config, values, use_llm, **kwargs):
        assert values == [record]
        return [deepcopy(draft)]
    assert draft_report_items(config, [record], True, original)[0].draft_fields == draft.draft_fields


def test_native_cache_restores_reviewed_selection_and_missing_derived_crop(native_supported):
    record, draft, config = native_supported
    save_native_cache(record, draft, config)
    crop = Path(record.reading['paper_visual_assets']['assets'][0]['path'])
    expected = crop.read_bytes()
    crop.unlink()
    fresh = deepcopy(record)
    fresh.reading = {}
    fresh.detail = {}
    hit = _load(config, fresh)
    assert hit is not None and crop.read_bytes() == expected
    cached, restored = hit
    assert _core_cache_supported(cached, restored, config=config)
    assert not _supported(cached, restored, config=config)
    assert cached.raw['paper_visual_selection'] == record.raw['paper_visual_selection']
    assert cached.detail == draft.draft_fields


def test_native_cache_never_overwrites_changed_source_or_review(native_supported):
    record, draft, config = native_supported
    save_native_cache(record, draft, config)
    path = Path(record.paper_document['source_pdf_path'])
    changed = path.read_bytes() + b'\nchanged-source'
    path.write_bytes(changed)
    assert _load(config, record) is None
    assert path.read_bytes() == changed


def test_native_selection_mutation_does_not_inherit_cached_review(native_supported):
    record, draft, config = native_supported
    save_native_cache(record, draft, config)
    record.raw['paper_visual_selection']['assets'][0]['caption'] = 'An unsupported new result'
    assert _load(config, record) is None


def test_native_slots_are_finite_and_never_enable_whole_page_work(native_supported):
    record, _, config = native_supported
    frozen = [candidate(record)]
    with Execution(config.root / 'issue', 'native-slots', frozen, config, protocol='offline-policy') as execution:
        slots = execution._slots if hasattr(execution, '_slots') else None
        from daily_agent.batch_execution import _slots
        values = list(_slots(execution.contract).values())
        assert not any(v[1] in {'visual', 'fidelity', 'repaired'} for v in values)
        assert [v for v in values if v[2] == 'native_visual_selection'] == [[record.key, 'primary_writer', 'native_visual_selection', 0]]
        limits = {name: pool['limit'] for name, pool in execution.snapshot()['pools'].items()}
        assert limits == {'native': 30, 'repaired': 30, 'visual': 20, 'fidelity': 40, 'primary_writer': 60}
    old = deepcopy(config)
    old.sources['reading'].pop('source_evidence_policy')
    with pytest.raises(ExecutionConflict):
        with Execution(config.root / 'issue', 'native-slots', frozen, old, protocol='offline-policy'):
            pass


def test_enrichment_reuse_never_restores_a_different_source(native_supported):
    from daily_agent.deferred_review_cache import reusable_enrichment
    record, draft, config = native_supported
    save_native_cache(record, draft, config)
    assert reusable_enrichment(config, record)
    source = Path(record.paper_document['source_pdf_path'])
    changed = source.read_bytes() + b'\nnew-unverified-source-bytes'
    source.write_bytes(changed)
    assert not reusable_enrichment(config, record)
    assert source.read_bytes() == changed


@pytest.mark.parametrize('format', ['html', 'markdown', 'editorial'])
def test_native_public_renderers_cannot_reuse_review_for_modified_fields(native_supported, format):
    from datetime import date
    from daily_agent.models import RunStatus
    record, draft, config = native_supported
    item = ApprovedItem(record.key, 'paper', record.title, record.source, record.url,
                        deepcopy(draft.draft_fields), record)
    item.final_fields['key_result'] += ' FORGED_RESULT_99%'
    if format == 'html':
        from daily_agent.rendering.html import _render_papers
        render = lambda: _render_papers([item], {record.key: 1}, date(2026, 10, 10))
    elif format == 'markdown':
        from daily_agent.rendering.markdown import _render_papers
        render = lambda: _render_papers([item], {record.key: 1}, date(2026, 10, 10))
    else:
        from daily_agent.rendering.editorial import render_editorial_html
        render = lambda: render_editorial_html([item], date(2026, 10, 10))
    with pytest.raises(ValueError, match='independent review'):
        render()


def test_native_ready_seal_rejects_changed_fields_without_new_pointer(native_supported):
    from datetime import date
    from daily_agent.scheduling import seal_ready_report, StageFailure
    from daily_agent.workflow_state import atomic_json
    record, draft, config = native_supported
    config.state_dir = config.root / 'data/state'
    config.delivery = {}
    config.quota = {}
    day = date(2026, 10, 10)
    report = config.reports_dir / f'daily-agent-{day}.md'
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('Offline synthetic report fixture, never delivered.')
    approved = row(record, draft)
    approved['final_fields']['key_result'] += ' FORGED_RESULT_99%'
    atomic_json(config.root / 'data/editorial' / str(day) / 'approval.json', [approved])
    with pytest.raises(StageFailure, match='independent review'):
        seal_ready_report(config, day)
    assert not (config.state_dir / 'ready-reports' / f'{day}.json').exists()


@pytest.mark.parametrize('erase_pixel_proof', [False, True])
def test_native_policy_cannot_downgrade_to_forged_legacy_flags(native_supported, erase_pixel_proof):
    record, draft, config = native_supported
    record.reading.pop('source_evidence')
    record.reading['visual'] = {'strict_fidelity': True, 'fidelity': {'passed': True}}
    if erase_pixel_proof:
        record.reading.pop('native_visual_evidence')
        record.reading['verification'].pop('native_claim_review')
        draft.verification.pop('native_claim_review')
    assert record_policy(record) == 'invalid'
    assert not native_quality(record, draft)
    assert not audit_reading([record.to_dict()])['all_passed']
    assert not _qualifying_rows([row(record, draft)])[0]  # configless archival audit too
    assert not _supported(record, draft, config=config)
