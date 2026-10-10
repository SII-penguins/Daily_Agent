"""Synthetic caption-role regressions with full-page scans and exact spans.

No original publication text or real pixel-inspection receipts are embedded.
Synthetic PDFs below test byte/provenance invariants only.
"""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_agent import native_visual_evidence as native
from daily_agent.paper_visual_assets import (
    MAX_ASSETS, MAX_TOTAL_BYTES, _plot_coverage, _qualitative_coverage,
    prepare_visual_assets, verified_assets,
)
from daily_agent.visual_inventory import native_caption_inventory, native_nomination_plan
from test_native_claim_pixel_review import example, response_for_selection, select


@pytest.fixture
def synthetic_papers():
    path = Path(__file__).parent / 'fixtures/native_caption_trial_20261009.json.gz'
    return json.loads(gzip.decompress(path.read_bytes()))['papers']


def test_synthetic_late_qualitative_figure_is_nominated(synthetic_papers):
    original = synthetic_papers[0]
    doc = deepcopy(original['native_document'])
    before = deepcopy(doc)
    plan = native_nomination_plan(doc)
    assert 16 not in original['original_candidate_pages']
    assert plan['candidate_pages'] == [16, 2, 6, 8, 5, 4, 7, 15]
    assert len(plan['candidate_pages']) == len(set(plan['candidate_pages'])) == native.MAX_PAGES == 8
    inventory = plan['inventory']
    assert inventory['source_pdf_sha256'] == '8ff0a41e6231eedaa557d343c032e2d7b6b60b59a1d82e10f69e47b5f03ee2f3'
    assert inventory['scanned_native_pages'] == list(range(1, 21))
    assert inventory['complete_native_page_scan'] is True
    assert inventory['visual_inventory_complete'] is False
    figure = next(c for c in inventory['captions'] if c['kind'] == 'figure' and c['number'] == '4')
    assert figure['page'] == 16 and figure['role_hint'] == 'qualitative_result'
    assert 'eight invented scenarios' in figure['caption']
    assert 'blue lines represent predicted trajectories' in figure['caption']
    assert not any(c['role_hint'] == 'quantitative_plot' for c in inventory['captions'])
    omitted = plan['not_nominated_captions']
    assert {c['page'] for c in omitted} == {9, 14, 17}
    assert all(c['nomination_status'] == 'not_nominated_page_budget' and c['pixels_reviewed'] is False for c in omitted)
    assert doc == before  # Original source data and metadata remain untouched.


def test_synthetic_tradeoff_plot_is_distinct_from_dataset_statistics(synthetic_papers):
    plan = native_nomination_plan(synthetic_papers[1]['native_document'])
    figures = {c['number']: c for c in plan['inventory']['captions'] if c['kind'] == 'figure'}
    assert figures['2']['role_hint'] == 'descriptive_figure'
    assert figures['3']['role_hint'] == 'quantitative_plot'
    assert plan['candidate_pages'][0] == 8
    assert figures['1']['page'] in plan['candidate_pages']
    assert 7 in plan['candidate_pages']
    assert len(plan['candidate_pages']) == 8
    assert len(figures) == 4  # “Figure 3 illustrates ...” is not another caption.


def test_inventory_retains_exact_native_spans_and_is_order_independent(synthetic_papers):
    for paper in synthetic_papers:
        doc = paper['native_document']
        plan = native_nomination_plan(doc)
        by_page = {p['page']: p['text'] for p in doc['pages']}
        for candidate in plan['inventory']['captions']:
            text = by_page[candidate['page']]
            start, stop = candidate['text_span']
            assert candidate['caption'] == text[start:stop]
            assert candidate['native_text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
        shuffled = {**doc, 'pages': list(reversed(doc['pages']))}
        assert native_nomination_plan(shuffled) == plan


def test_role_diversity_survives_many_early_tables_without_magic_page_numbers():
    pages = [{'page': n, 'text': f'Table {n}. Overall benchmark performance.'} for n in range(1, 31)]
    pages += [
        {'page': 31, 'text': 'Figure 1. Dataset and tool statistics.'},
        {'page': 32, 'text': 'Figure 2. Overall accuracy across models.'},
        {'page': 33, 'text': 'Figure 3. Qualitative results with predicted trajectories.'},
        {'page': 34, 'text': 'Figure 4. System overview and framework.'},
        {'page': 35, 'text': 'Table 31. Ablation of component contributions.'},
        {'page': 36, 'text': 'The loss is:\nL = a + b\n(1)\n'},
    ]
    plan = native_nomination_plan({'source_page_count': 36, 'pages': pages})
    assert set(range(31, 37)) <= set(plan['candidate_pages'])
    assert len(plan['candidate_pages']) == 8
    assert plan['candidate_pages'][:3] == [32, 33, 34]
    assert plan['not_nominated_captions']


def test_references_objective_prose_and_absent_captions_do_not_invent_visuals():
    doc = {'source_page_count': 3, 'pages': [
        {'page': 1, 'text': 'See Figure 4 for results.\nTable 7, the model improves.\nFigure 3 illustrates the comparison.'},
        {'page': 2, 'text': 'The objective is to minimize the average planning error.'},
        {'page': 3, 'text': 'We describe an unnumbered image in prose, with no detected caption.'},
    ]}
    plan = native_nomination_plan(doc)
    assert plan['candidate_pages'] == []
    assert plan['inventory']['captions'] == []
    assert plan['inventory']['numbered_math_candidate_pages'] == []
    assert plan['absence_status'] == 'unknown_until_supplied_pixels_reviewed'
    assert plan['inventory']['complete_native_page_scan'] is True
    assert plan['inventory']['visual_inventory_complete'] is False
    assert 'absent from the paper' in plan['scope']


def test_missing_native_pages_and_note_only_fallback_remain_unreviewed():
    doc = {'source_page_count': 3, 'pages': [{'page': 1, 'text': 'Introduction.'}, {'page': 3, 'text': 'Results.'}]}
    notes = [{'chunk_id': 'c1', 'summary': 'See Figure 8'}, {'chunk_id': 'c2', 'summary': 'See Figure 9'}]
    chunks = [{'id': 'c1', 'page': 3}, {'id': 'c2', 'page': 2}]
    plan = native_nomination_plan(doc, notes, chunks)
    assert plan['candidate_pages'] == [3]
    assert plan['inventory']['complete_native_page_scan'] is False
    assert plan['candidates'] == [{'page': 3, 'role_hints': ['reading_note_reference'],
                                  'caption_ids': [], 'pixels_reviewed': False}]


@pytest.mark.parametrize('limit', [-1, 9, True, 1.5])
def test_nomination_cannot_expand_pixel_budget(limit):
    with pytest.raises(ValueError, match='budget'):
        native_nomination_plan({'pages': []}, limit=limit)


def test_duplicate_source_page_is_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        native_caption_inventory({'pages': [{'page': 1, 'text': ''}, {'page': 1, 'text': ''}]})


def test_caption_scan_never_opens_images_or_pdf(synthetic_papers, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Nomination must use native text only')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    for paper in synthetic_papers:
        record = SimpleNamespace(paper_document=paper['native_document'], reading={'notes': []})
        assert native.nominate_pages(record) == native.nomination_plan(record)['candidate_pages']


def qualitative_selection():
    return {'selection_policy_version': 2,
        'assets': [{'kind': 'result_figure', 'number': 'Figure 4', 'figure_role': 'qualitative_result'}],
        'experiment_plot_coverage': {'reviewed': True, 'available': False,
            'key_plot_numbers': [], 'absence_reason': 'Only qualitative trajectory overlays in supplied pixels'},
        'qualitative_result_coverage': {'reviewed': True, 'available': True,
            'key_figure_numbers': ['Figure 4']}}


def test_qualitative_result_does_not_fabricate_quantitative_plot():
    selection = qualitative_selection()
    assert _plot_coverage(selection, selection['assets']) == []
    assert _qualitative_coverage(selection, selection['assets'], required=True) == []
    assert '定性' not in ''.join(native._display_gaps({**selection, 'gaps': []}))


@pytest.mark.parametrize('mutation', ['missing', 'not_reviewed', 'false_absence', 'no_keys', 'wrong_keys', 'omitted_without_reason', 'invalid_role', 'null_role', 'framework_role'])
def test_qualitative_coverage_fails_closed(mutation):
    selection = qualitative_selection()
    if mutation == 'missing': selection.pop('qualitative_result_coverage')
    elif mutation == 'not_reviewed': selection['qualitative_result_coverage']['reviewed'] = False
    elif mutation == 'false_absence': selection['qualitative_result_coverage']['available'] = False
    elif mutation == 'no_keys': selection['qualitative_result_coverage']['key_figure_numbers'] = []
    elif mutation == 'wrong_keys': selection['qualitative_result_coverage']['key_figure_numbers'] = ['Figure 9']
    elif mutation == 'omitted_without_reason': selection['assets'] = []
    elif mutation == 'null_role': selection['assets'][0]['figure_role'] = None
    elif mutation == 'framework_role': selection['assets'][0]['kind'] = 'framework'
    else: selection['assets'][0]['figure_role'] = 'performance_guarantee'
    with pytest.raises(ValueError):
        _plot_coverage(selection, selection['assets'])
        _qualitative_coverage(selection, selection['assets'], required=True)


def test_explicit_byte_budget_omission_is_distinct_from_absence():
    selection = qualitative_selection()
    selection['assets'] = []
    selection['gaps'] = ['Original qualitative crop does not fit with other selected evidence within aggregate byte budget']
    selection['qualitative_result_coverage']['omission_reason'] = 'Original complete labeled crop cannot fit within the remaining byte budget'
    assert _qualitative_coverage(selection, [])
    display = native._display_gaps(selection)
    assert any('1 项定性实验结果图未纳入' in gap for gap in display)
    assert selection['qualitative_result_coverage']['available'] is True


def test_payload_exposes_scope_and_unchanged_budgets(example):
    record, _, _ = example
    payload = native._selection_payload(record, [])
    assert payload['budgets'] == {'max_pages': 8, 'max_crops': MAX_ASSETS,
                                  'max_crop_bytes': MAX_TOTAL_BYTES, 'crop_raster_scale': 2}
    assert MAX_ASSETS == 5 and MAX_TOTAL_BYTES == 2_000_000
    assert payload['native_caption_nomination']['inventory']['visual_inventory_complete'] is False


def test_figure_role_retains_original_crop_bytes_and_source_binding(example):
    record, _, config = example
    job = select(example)
    before = verified_assets(record)
    hashes = [(asset['source_pdf_sha256'], asset['page_image_sha256'], asset['artifact_sha256'], data)
              for asset, data in before]
    selection = response_for_selection(job)
    selection['assets'][0]['figure_role'] = 'qualitative_result'
    selection['experiment_plot_coverage'] = {'reviewed': True, 'available': False,
        'key_plot_numbers': [], 'absence_reason': 'Synthetic protocol test, no quantitative classification'}
    selection['qualitative_result_coverage'] = {'reviewed': True, 'available': True,
        'key_figure_numbers': ['Figure 1']}
    record.raw['paper_visual_selection'] = selection
    prepare_visual_assets(record, config.reports_dir, {'paper_visual_assets_enabled': True})
    after = verified_assets(record)
    assert record.reading['paper_visual_assets']['status'] == 'ready'
    assert after[0][0]['figure_role'] == 'qualitative_result'
    assert [(asset['source_pdf_sha256'], asset['page_image_sha256'], asset['artifact_sha256'], data)
            for asset, data in after] == hashes
    # A changed semantic role invalidates the old selection receipt, even if
    # the original PNG bytes themselves have not changed.
    assert native.verified_native_visual_evidence(record) is False
