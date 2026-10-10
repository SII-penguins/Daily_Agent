"""Offline original-PDF and real-parent-queue tests; never call a model."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_agent.models import MaterialRecord, EditorialDraft
from daily_agent.paper_document import digest
from daily_agent.parent_writer import request, import_response, pending, PendingResponse
from daily_agent.native_visual_evidence import (
    prepare_native_visual_evidence, verified_native_visual_evidence, nominate_pages,
    SELECT_PROMPT, page_pixels, source,
)
from daily_agent.native_claim_review import (
    review_native_claims, native_claim_support_valid, build_page_evidence,
    validate_page_evidence, REVIEW_PROMPT,
)


@pytest.fixture
def example(tmp_path):
    fitz = pytest.importorskip('fitz')
    path = tmp_path / 'paper.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page(width=380, height=260)
        page.insert_text((20, 30), 'A benchmark problem compares simulated policies.')
        page.insert_text((20, 65), 'The method uses an adaptive policy update.')
        page.insert_text((20, 110), 'Table 1. Simulated success rate (%)')
        page.insert_text((20, 140), 'Ours 81.2   Baseline 72.0')
        page.insert_text((20, 190), 'Figure 1. Success rate curve across training steps')
        page.draw_line((30, 240), (250, 205))
        pdf.save(path)
    with fitz.open(path) as pdf:
        text = pdf[0].get_text()
    doc = {'source_type': 'pdf', 'document_kind': 'full_text', 'source_pdf_path': str(path),
           'source_pdf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'source_page_count': 1,
           'identity': 'paper-version-1', 'source_url': 'https://example.org/paper.pdf',
           'pages': [{'page': 1, 'text': text}],
           'chunks': [{'id': 'c1', 'page': 1, 'text': text, 'section': 'Results', 'kind': 'results'}]}
    record = MaterialRecord(key='paper-one', source='arxiv', item_type='paper', title='Benchmark policies',
        url='https://example.org/paper', paper_document=doc,
        reading={'complete': True, 'read_chunk_ids': ['c1'], 'notes': []},
        paper_text_status={'sufficient_for_deep_summary': True})
    config = SimpleNamespace(root=tmp_path, reports_dir=tmp_path / 'reports',
        sources={'reading': {'timeout_seconds': 45}, 'llm_writer': {'provider': 'parent_queue'}})
    draft = EditorialDraft(key=record.key, item_type='paper', title=record.title,
        draft_fields={'problem': 'A benchmark compares simulated policies.',
                      'method': 'The method uses an adaptive policy update.',
                      'key_result': 'Simulated success rate is 81.2 for Ours and 72.0 for Baseline.',
                      'confidence': 'high'},
        claim_evidence=[{'field': f, 'chunk_id': 'c1', 'quote': q, 'evidence_kind': 'simulation',
                         'conditions': 'Simulated success rate (%), Ours vs Baseline'}
                        for f, q in [('problem', 'A benchmark problem compares simulated policies.'),
                                     ('method', 'The method uses an adaptive policy update.'),
                                     ('key_result', 'Ours 81.2   Baseline 72.0')]],
        verification={'status': 'located', 'valid_fields': ['key_result', 'method', 'problem'],
                      'issues': [], 'semantic_support': 'not_independently_verified'})
    return record, draft, config


def invoke(config):
    def call(prompt, timeout, image_path=None, **kwargs):
        return request(config.root, prompt, timeout, image_path=image_path, **kwargs)
    return call


def response_for_selection(job):
    payload = json.loads(job['prompt'][len(SELECT_PROMPT):])
    page = payload['pages'][0]
    return {'schema_version': 1, 'selection_policy_version': 2,
            'source_pdf_sha256': payload['source_pdf_sha256'], 'assets': [
                {'kind': 'result_figure', 'number': 'Figure 1', 'page': 1, 'bbox': [15, 170, 290, 250],
                 'page_image_sha256': page['page_image_sha256'], 'caption': '训练步数中的成功率曲线',
                 'conditions': 'Simulation; original plot axes retained',
                 'review': {'labels_checked': True, 'conditions_checked': True}},
                {'kind': 'result_table', 'number': 'Table 1', 'page': 1, 'bbox': [15, 90, 290, 150],
                 'page_image_sha256': page['page_image_sha256'], 'caption': '模拟成功率比较',
                 'conditions': 'Simulation success rate (%); Ours 81.2, Baseline 72.0',
                 'review': {'labels_checked': True, 'conditions_checked': True}}],
            'gaps': ['No framework was selected'],
            'experiment_plot_coverage': {'reviewed': True, 'available': True, 'key_plot_numbers': ['Figure 1']}}


def select(example):
    record, _, config = example
    with pytest.raises(PendingResponse):
        prepare_native_visual_evidence(record, config, invoke(config))
    jobs = pending(config.root)
    assert len(jobs) == 1 and jobs[0]['stage'] == 'visual_selection'
    result = response_for_selection(jobs[0])
    import_response(config.root, jobs[0]['job_id'], result, 'selector-worker')
    state = prepare_native_visual_evidence(record, config, invoke(config))
    assert state['status'] == 'ready'
    assert verified_native_visual_evidence(record)
    return jobs[0]


def response_for_review(job):
    payload = json.loads(job['prompt'][len(REVIEW_PROMPT):])
    return {'input_sha256': payload['input_sha256'],
            'checks': [{'field': f['field'], 'supported': True, 'reason': 'Offline fixture: source pixels support this claim',
                        'pixels_checked': True, 'image_sha256s': f['expected_image_sha256s'],
                        'evidence_sha256': f['evidence_sha256']} for f in payload['fields']],
            'asset_checks': [{'artifact_sha256': a['artifact_sha256'], 'supported': True,
                              'pixels_checked': True, 'reason': 'Offline fixture: exact caption and conditions checked',
                              'text_sha256': a['text_sha256']} for a in payload['selected_assets']]}


def queue_review(example):
    record, draft, config = example
    with pytest.raises(PendingResponse):
        review_native_claims(draft, record, invoke(config), 45)
    jobs = pending(config.root)
    assert len(jobs) == 1 and jobs[0]['stage'] == 'review'
    return jobs[0]


def approve(example):
    select(example)
    record, draft, config = example
    job = queue_review(example)
    import_response(config.root, job['job_id'], response_for_review(job), 'independent-reviewer')
    review_native_claims(draft, record, invoke(config), 45)
    assert native_claim_support_valid(record, draft)
    return job


def test_real_queue_preserves_original_crops_and_exact_claim_context(example):
    job = approve(example)
    record, draft, config = example
    payload = json.loads(job['prompt'][len(REVIEW_PROMPT):])
    assert len(job['images']) == 3  # one actual source page and two original crops
    assert payload['page_evidence']['source_chunks'] == record.paper_document['chunks']
    assert payload['draft']['draft_fields'] == draft.draft_fields
    assert draft.verification['native_claim_review']['receipt']['worker_id'] == 'independent-reviewer'
    assert 'strict_fidelity' not in record.reading.get('visual', {})
    assert not record.reading.get('visual', {}).get('fidelity')
    assert record.reading['native_visual_evidence']['candidate_pages'][0]['page'] == 1
    assert not any('No framework' in g for g in record.reading['paper_visual_assets']['gaps'])


def test_actual_queue_disallows_selector_as_independent_claim_reviewer(example):
    select(example)
    _, _, config = example
    job = queue_review(example)
    with pytest.raises(ValueError, match='different worker'):
        import_response(config.root, job['job_id'], response_for_review(job), 'selector-worker')


@pytest.mark.parametrize('mutation', ['fields', 'claims', 'document', 'source', 'page', 'crop', 'caption', 'review_answer', 'worker'])
def test_acceptance_rechecks_exact_source_draft_pixels_and_receipt(example, mutation):
    job = approve(example)
    record, draft, config = example
    if mutation == 'fields': draft.draft_fields['key_result'] += ' Guaranteed everywhere.'
    elif mutation == 'claims': draft.claim_evidence[0]['conditions'] = 'Hardware, not simulation'
    elif mutation == 'document': record.paper_document['chunks'][0]['text'] += 'hidden extra text'
    elif mutation == 'source': Path(record.paper_document['source_pdf_path']).write_bytes(b'%PDF modified')
    elif mutation == 'page':
        path = config.root / draft.verification['native_claim_review']['input']['images'][0]['path']
        path.write_bytes(b'changed pixels')
    elif mutation == 'crop': Path(record.reading['paper_visual_assets']['assets'][0]['path']).write_bytes(b'changed crop')
    elif mutation == 'caption': record.reading['paper_visual_assets']['assets'][0]['caption'] = 'Invented 99% performance'
    else:
        path = config.root / 'data/writer-queue' / f"{job['job_id']}.answer.json"
        value = json.loads(path.read_text())
        if mutation == 'worker': value['worker_id'] = 'selector-worker'
        else: value['response']['checks'][0]['reason'] = 'Altered after acceptance'
        path.write_text(json.dumps(value))
    assert not native_claim_support_valid(record, draft)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_hash', 'false_pixels', 'bad_input', 'asset_missing', 'asset_text'])
def test_incomplete_or_unbound_responses_never_pass(example, mutation):
    select(example)
    record, draft, config = example
    job = queue_review(example)
    response = response_for_review(job)
    if mutation == 'missing': response['checks'].pop()
    elif mutation == 'duplicate': response['checks'][0] = deepcopy(response['checks'][1])
    elif mutation == 'wrong_hash': response['checks'][0]['image_sha256s'] = ['0' * 64]
    elif mutation == 'false_pixels': response['checks'][0]['pixels_checked'] = False
    elif mutation == 'bad_input': response['input_sha256'] = '0' * 64
    elif mutation == 'asset_missing': response['asset_checks'].pop()
    elif mutation == 'asset_text': response['asset_checks'][0]['text_sha256'] = '0' * 64
    import_response(config.root, job['job_id'], response, 'independent-reviewer')
    review_native_claims(draft, record, invoke(config), 45)
    assert draft.verification['semantic_support'] == 'review_failed'
    assert not native_claim_support_valid(record, draft)


def test_unsupported_result_removed_and_gate_fails(example):
    select(example)
    record, draft, config = example
    job = queue_review(example)
    response = response_for_review(job)
    result = next(c for c in response['checks'] if c['field'] == 'key_result')
    result.update(supported=False, reason='Pixel table aligns the value with a different baseline')
    import_response(config.root, job['job_id'], response, 'independent-reviewer')
    review_native_claims(draft, record, invoke(config), 45)
    assert draft.draft_fields['key_result'] == 'not_stated'
    assert all(c['field'] != 'key_result' for c in draft.claim_evidence)
    assert not native_claim_support_valid(record, draft)


def test_fake_invoke_response_has_no_independent_provenance(example):
    select(example)
    record, draft, _ = example
    def fake(prompt, timeout, image_path):
        return response_for_review({'prompt': prompt})
    review_native_claims(draft, record, fake, 45)
    assert not native_claim_support_valid(record, draft)
    assert draft.verification['semantic_support'] == 'review_failed'


def test_no_model_mode_does_not_enqueue(example):
    record, draft, config = example
    prepare_native_visual_evidence(record, config, None)
    review_native_claims(draft, record, None, 45)
    assert not pending(config.root)
    assert not verified_native_visual_evidence(record)


def test_reusable_scientific_page_helper_is_read_only_on_validation(example):
    record, _, config = example
    paths, manifest = build_page_evidence(record, ['c1'], config.root)
    assert len(paths) == 1 and validate_page_evidence(record, manifest)
    Path(paths[0]).unlink()
    assert not validate_page_evidence(record, manifest)
    assert not Path(paths[0]).exists()


def test_no_caption_pages_is_honest_empty_not_plot_absence(example):
    record, _, config = example
    record.paper_document['pages'][0]['text'] = 'Plain prose without a figure caption.'
    state = prepare_native_visual_evidence(record, config, invoke(config))
    assert state['status'] == 'empty' and verified_native_visual_evidence(record)
    assert record.raw['paper_visual_selection']['experiment_plot_coverage']['available'] is None
    assert not pending(config.root)
    record.raw['paper_visual_selection']['gaps'] = ['No plots exist in this paper']
    assert not verified_native_visual_evidence(record)


def test_source_containment_and_nested_cross_source_are_rejected(example, tmp_path):
    record, _, config = example
    outside = tmp_path.parent / (tmp_path.name + '-external.pdf')
    outside.write_bytes(Path(record.paper_document['source_pdf_path']).read_bytes())
    record.paper_document['source_pdf_path'] = str(outside)
    with pytest.raises(ValueError): source(record, config.root)
    record.paper_document['source_pdf_path'] = str(config.root / 'paper.pdf')
    record.paper_document['native_document'] = deepcopy(record.paper_document)
    record.paper_document['native_document']['identity'] = 'different-paper'
    with pytest.raises(ValueError, match='nesting'): source(record, config.root)


def test_nomination_and_claim_page_budgets_are_finite(example):
    record, _, config = example
    record.paper_document['pages'] = [{'page': n, 'text': f'Figure {n}. Success rate curve'} for n in range(1, 20)]
    assert len(nominate_pages(record)) == 8
    record.paper_document['chunks'] = [{'id': f'c{n}', 'page': n, 'text': 'Full original context here'} for n in range(1, 10)]
    record.reading['read_chunk_ids'] = [f'c{n}' for n in range(1, 10)]
    with pytest.raises(ValueError, match='over-bound'):
        build_page_evidence(record, record.reading['read_chunk_ids'], config.root)
    assert not pending(config.root)


def test_control_character_quote_does_not_get_guessed_from_text(example):
    select(example)
    record, draft, config = example
    record.paper_document['chunks'][0]['text'] += '\nOurs 99\x00.9 for an invisible table cell'
    draft.claim_evidence[-1]['quote'] = 'Ours 99\x00.9 for an invisible table cell'
    review_native_claims(draft, record, invoke(config), 45)
    assert not native_claim_support_valid(record, draft)
    assert not pending(config.root)


def test_recomputed_visual_hash_cannot_omit_or_relabel_selected_asset(example):
    from daily_agent.native_visual_evidence import _asset_integrity
    select(example)
    record, _, config = example
    original = deepcopy(record.reading)
    record.reading['paper_visual_assets']['assets'].pop()
    with pytest.raises(ValueError, match='integrity'):
        _asset_integrity(record, config.root, config.reports_dir, [1])
    record.reading = original
    asset = record.reading['paper_visual_assets']['assets'][0]
    asset['caption'] = 'Invented 100% universal success'
    asset['binding_sha256'] = digest({k: v for k, v in asset.items() if k not in {'path', 'binding_sha256'}})
    with pytest.raises(ValueError, match='selected crop/text'):
        _asset_integrity(record, config.root, config.reports_dir, [1])


def test_recomputed_unknown_or_boolean_schema_is_rejected(example):
    approve(example)
    record, draft, _ = example
    manifest = draft.verification['native_claim_review']
    for schema in (2, True, '1'):
        manifest['schema_version'] = schema
        manifest['binding_sha256'] = digest({k: v for k, v in manifest.items() if k != 'binding_sha256'})
        assert not native_claim_support_valid(record, draft)


def test_missing_queue_receipt_cannot_be_replaced_with_model_booleans(example):
    job = approve(example)
    record, draft, config = example
    (config.root / 'data/writer-queue' / f"{job['job_id']}.answer.json").unlink()
    assert not native_claim_support_valid(record, draft)


def test_native_selection_and_claim_queue_use_exact_finite_slots(example):
    from daily_agent.batch_execution import Execution, candidate
    from daily_agent.native_visual_evidence import POLICY
    record, draft, config = example
    config.sources['reading'].update(source_evidence_policy=POLICY, run_budget_seconds=30.0,
        visual_budget_seconds=20.0, fidelity_budget_seconds=40.0, max_chunks_per_paper=80)
    config.sources['llm_writer']['run_budget_seconds'] = 60.0
    frozen = candidate(record)
    with Execution(config.root / 'issue', 'native-fixture-batch', [frozen], config,
                   protocol='native-offline-fixture-v1') as execution:
        with pytest.raises(PendingResponse):
            prepare_native_visual_evidence(record, config, invoke(config), execution=execution)
        job = pending(config.root)[0]
        operation = execution.existing_operation((record.key, 'primary_writer', 'native_visual_selection', 0))
        assert operation['queue_job_id'] == job['job_id'] and operation['queue_role'] == 'visual_selection'
        assert 0 < job['call_timeout_seconds'] <= 45
        import_response(config.root, job['job_id'], response_for_selection(job), 'selector-worker')
        prepare_native_visual_evidence(record, config, invoke(config), execution=execution)
        assert verified_native_visual_evidence(record)
        with pytest.raises(PendingResponse):
            review_native_claims(draft, record, invoke(config), 45, execution=execution)
        job = pending(config.root)[0]
        operation = execution.existing_operation((record.key, 'primary_writer', 'semantic', 0))
        assert operation['queue_job_id'] == job['job_id'] and operation['queue_role'] == 'review'
        import_response(config.root, job['job_id'], response_for_review(job), 'independent-reviewer')
        review_native_claims(draft, record, invoke(config), 45, execution=execution)
        assert native_claim_support_valid(record, draft)
        operations = execution.snapshot()['operations']
        assert len(operations) == 2
        assert all(v['identity'][1] == 'primary_writer' for v in operations.values())


def test_declared_plot_omissions_remain_visible_without_publishing_unreviewed_prose(example):
    from daily_agent.native_visual_evidence import _display_gaps
    select(example)
    record, _, _ = example
    selection = deepcopy(record.raw['paper_visual_selection'])
    selection['experiment_plot_coverage']['key_plot_numbers'] = ['Figure 1', 'Figure 2', 'Figure 3']
    selection['gaps'] = []
    gaps = _display_gaps(selection)
    assert any('2 项实验图未纳入' in gap for gap in gaps)
    assert not any('论文没有' in gap for gap in gaps)
