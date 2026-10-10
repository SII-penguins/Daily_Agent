"""Synthetic failure-shape regressions plus real local queue/PDF contracts.

These deterministic replies test mechanics, not real scientific model quality.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_agent.batch_execution import Execution, BudgetExhausted, candidate
from daily_agent.models import EditorialDraft
from daily_agent.native_claim_review import (
    CitationPageBudgetError, review_plan, review_native_claims,
    native_claim_support_valid, writer_citation_contract, REVIEW_PROMPT,
)
from daily_agent.parent_writer import PendingResponse, import_response, pending
from daily_agent.paper_document import digest
from daily_agent.reading import CORE
from daily_agent.scientific_analysis import (
    analysis_diagnostics, validate_analysis, analyze_papers, scientific_analysis_valid,
)
from test_native_claim_pixel_review import example, select, invoke, response_for_review
from test_native_scientific_pixels import native_science, review_response
from test_stage_execution import StageClock


@pytest.fixture
def observed():
    return json.loads((Path(__file__).parent / 'fixtures/native_post_trial_failures_20261009.json').read_text())


def test_synthetic_scientific_rejection_reports_only_missing_time_horizon(observed):
    value = observed['scientific']
    record = SimpleNamespace(paper_document={'chunks': value['chunks']},
                             reading={'read_chunk_ids': value['read_chunk_ids']})
    issues = analysis_diagnostics(value['analysis'], record)
    assert len(issues) == 1
    assert issues[0]['claim_id'] == 'synthetic_component_interaction'
    assert issues[0]['missing_numeric_tokens'] == ['3']
    assert not validate_analysis(value['analysis'], record)


def test_synthetic_nine_page_rewrite_fits_two_intact_review_packs_without_losing_evidence(observed):
    value = observed['rewrite']
    record = SimpleNamespace(paper_document={'chunks': value['chunks']},
                             reading={'read_chunk_ids': value['read_chunk_ids']})
    draft = EditorialDraft.from_dict({'item_type': 'paper', 'title': 'Synthetic immutable rewrite',
        **value['draft'], 'verification': {'valid_fields': list(CORE)}})
    before = deepcopy(draft.to_dict())
    plan = review_plan(draft, record)
    assert len(plan['packs']) == 2
    assert all(len(pack['pages']) <= 8 for pack in plan['packs'])
    assert sorted({p for pack in plan['packs'] for p in pack['pages']}) == [1, 2, 4, 5, 6, 7, 8, 9, 14]
    fields = [f for pack in plan['packs'] for f in pack['fields']]
    assert sorted(fields) == sorted(CORE) and len(fields) == len(set(fields))
    for pack in plan['packs']:
        assert all(set(plan['field_pages'][field]) <= set(pack['pages']) for field in pack['fields'])
    assert draft.to_dict() == before  # no dropping either a claim or citation
    contract = writer_citation_contract(record)
    assert contract['max_pages_per_review_pack'] == 8 and contract['max_review_packs'] == 2
    assert contract['chunk_pages'] == {c['id']: c['page'] for c in value['chunks']}


@pytest.fixture
def multipage(example):
    record, draft, config = example
    import fitz
    path = Path(record.paper_document['source_pdf_path'])
    fields = sorted(CORE)
    texts = {1: 'A benchmark problem compares simulated policies.'}
    with fitz.open(path) as pdf:
        for number in range(2, 10):
            page = pdf.new_page(width=380, height=260)
            text = f'Controlled simulation evidence on benchmark page {number} supports the bounded comparison.'
            page.insert_textbox(fitz.Rect(20, 20, 360, 200), text, fontsize=10)
            texts[number] = text
        pdf.saveIncr()
    with fitz.open(path) as pdf:
        record.paper_document['pages'] = [{'page': i + 1, 'text': p.get_text()} for i, p in enumerate(pdf)]
    record.paper_document.update(source_page_count=9, source_pdf_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    record.paper_document['chunks'] = [{'id': f'c{p}', 'page': p, 'text': text, 'section': 'Results'} for p, text in texts.items()]
    record.reading['read_chunk_ids'] = [f'c{p}' for p in texts]
    draft.draft_fields = {'confidence': 'high'}
    draft.claim_evidence = []
    for page, field in enumerate(fields, 1):
        draft.draft_fields[field] = [texts[page]] if field == 'method_steps' else texts[page]
        draft.claim_evidence.append({'field': field, 'chunk_id': f'c{page}', 'quote': texts[page],
            'conditions': 'Controlled simulation only', 'evidence_kind': 'simulation'})
    draft.verification = {'status': 'located', 'valid_fields': fields, 'issues': [],
                          'semantic_support': 'not_independently_verified'}
    config.sources['reading'].update(source_evidence_policy='native_claim_evidence_v1', run_budget_seconds=30,
        visual_budget_seconds=30, fidelity_budget_seconds=30, max_chunks_per_paper=20)
    config.sources['llm_writer'].update(run_budget_seconds=60)
    select(example)
    return example


def complete_multipack(value, factory=None):
    record, draft, config = value
    def run():
        if factory:
            with factory() as execution:
                return review_native_claims(draft, record, invoke(config), 45, execution=execution)
        return review_native_claims(draft, record, invoke(config), 45)
    jobs = []
    for index in range(2):
        with pytest.raises(PendingResponse): run()
        assert not native_claim_support_valid(record, draft)
        job = pending(config.root)[0]
        payload = json.loads(job['prompt'][len(REVIEW_PROMPT):])
        assert payload['pack_index'] == index
        assert len(payload['images']) <= 8
        assert bool(payload['selected_assets']) == (index == 0)
        jobs.append(job)
        import_response(config.root, job['job_id'], response_for_review(job), 'independent-reviewer')
    run()
    assert native_claim_support_valid(record, draft)
    return jobs, run


def test_two_queue_packs_require_all_pages_fields_assets_and_finite_shared_slots(multipage):
    record, draft, config = multipage
    frozen, clock = [candidate(record)], StageClock()
    def factory():
        return Execution(config.root / 'issue', 'two-native-packs', frozen, config, protocol='offline-packs', clock=clock)
    jobs, run = complete_multipack(multipage, factory)
    manifest = draft.verification['native_claim_review']
    assert manifest['schema_version'] == 2 and manifest['review_mode'] == 'native_claim_review_packs_v2'
    assert len(manifest['packs']) == 2
    assert {p['page'] for pack in manifest['packs'] for p in pack['input']['images']} == set(range(1, 10))
    assert sorted(c['field'] for c in draft.verification['semantic_checks']) == sorted(CORE)
    assert sum(len(p['input']['selected_assets']) for p in manifest['packs']) == len(record.reading['paper_visual_assets']['assets'])
    with factory() as active:
        identities = {tuple(op['identity']) for op in active.snapshot()['operations'].values()}
        assert identities == {(record.key, 'primary_writer', 'semantic', 0),
                              (record.key, 'primary_writer', 'semantic_overflow', 0)}
        assert active.snapshot()['writer_circuit'] is None
        assert active.snapshot()['pools']['primary_writer']['limit'] == 60
        with active.stage('primary_writer'):
            with pytest.raises(BudgetExhausted):
                active.admit(record.key, 'primary_writer', 'semantic_overflow', 2, {}, queue_job_id='third')
    before = sorted(p.name for p in (config.root / 'data/writer-queue').glob('*.job.json'))
    run()
    assert sorted(p.name for p in (config.root / 'data/writer-queue').glob('*.job.json')) == before


@pytest.mark.parametrize('mutation', ['missing_pack', 'duplicate_pack', 'swap_pack', 'draft', 'plan', 'pages',
    'asset', 'response', 'receipt', 'single_schema'])
def test_multipack_revalidation_rejects_tampering_even_if_outer_hash_resealed(multipage, mutation):
    record, draft, config = multipage
    complete_multipack(multipage)
    manifest = draft.verification['native_claim_review']
    if mutation == 'missing_pack': manifest['packs'].pop()
    elif mutation == 'duplicate_pack': manifest['packs'][1] = deepcopy(manifest['packs'][0])
    elif mutation == 'swap_pack': manifest['packs'].reverse()
    elif mutation == 'draft': draft.draft_fields['problem'] += ' Unsupported benefit.'
    elif mutation == 'plan': manifest['plan']['complete_draft_sha256'] = '0' * 64
    elif mutation == 'pages': manifest['packs'][0]['input']['images'].pop()
    elif mutation == 'asset': manifest['packs'][0]['input']['selected_assets'].pop()
    elif mutation == 'response': manifest['packs'][1]['response']['checks'][0]['supported'] = False
    elif mutation == 'receipt': manifest['packs'][0]['receipt']['worker_id'] = 'changed-worker'
    else: manifest['schema_version'] = 1
    manifest['binding_sha256'] = digest({k: v for k, v in manifest.items() if k != 'binding_sha256'})
    assert not native_claim_support_valid(record, draft)


def test_one_field_over_eight_pages_fails_before_render_or_queue(multipage):
    record, draft, config = multipage
    for ref in draft.claim_evidence:
        ref['field'] = 'key_result'
    for field in CORE:
        if field != 'key_result': draft.draft_fields[field] = ['not_stated'] if field == 'method_steps' else 'not_stated'
    draft.draft_fields['key_result'] = 'Controlled simulation evidence supports a bounded comparison.'
    draft.verification['valid_fields'] = ['key_result']
    before = sorted((config.root / 'data/writer-queue').glob('*.job.json'))
    failure = review_native_claims(draft, record, invoke(config), 45)
    assert failure['status'] == 'failed'
    assert failure['diagnostic']['cited_pages'] == list(range(1, 10))
    assert failure['diagnostic']['field'] == 'key_result'
    assert sorted((config.root / 'data/writer-queue').glob('*.job.json')) == before
    assert not native_claim_support_valid(record, draft)


def test_native_science_repairs_only_numeric_claim_once_then_independently_reviews(native_science):
    record, draft, config, analysis = native_science
    original = deepcopy(analysis)
    original['paragraphs'][1]['claims'][0]['text'] += '未获引证支持的时间条件3s。'
    frozen, clock = [candidate(record)], StageClock()
    def factory():
        return Execution(config.root / 'issue', 'local-science-repair', frozen, config, protocol='offline-local-repair', clock=clock)
    def run():
        with factory() as active:
            analyze_papers(config, [record], [draft], execution=active)
    with pytest.raises(PendingResponse): run()
    first = pending(config.root)[0]
    import_response(config.root, first['job_id'], original, 'scientific-author')
    with pytest.raises(PendingResponse): run()
    repair = pending(config.root)[0]
    data = json.loads(repair['prompt'].split('INPUT:\n', 1)[1])
    assert data['allowed_claim_ids'] == ['explanation']
    assert data['diagnostics'][0]['missing_numeric_tokens'] == ['3']
    for _ in range(2):
        with pytest.raises(PendingResponse): run()
        assert pending(config.root)[0]['job_id'] == repair['job_id']
    import_response(config.root, repair['job_id'], analysis, 'scientific-repair-author')
    with pytest.raises(PendingResponse): run()
    reviewer = pending(config.root)[0]
    assert reviewer['stage'] == 'review' and reviewer['images']
    import_response(config.root, reviewer['job_id'], review_response(reviewer), 'independent-science-reviewer')
    run()
    assert scientific_analysis_valid(record, draft)
    assert record.reading['scientific_analysis']['completion_scope'] == 'core_and_scientific_analysis'
    with factory() as active:
        state = active.snapshot()
        assert state['writer_circuit'] is None and state['pools']['primary_writer']['limit'] == 60
        assert {tuple(op['identity']) for op in state['operations'].values()} == {
            (record.key, 'primary_writer', 'scientific_writer', 0),
            (record.key, 'primary_writer', 'scientific_writer', 1),
            (record.key, 'primary_writer', 'scientific_review', 0)}
        with active.stage('primary_writer'):
            with pytest.raises(BudgetExhausted):
                active.admit(record.key, 'primary_writer', 'scientific_writer', 2, {}, queue_job_id='third')
    run()
    assert not pending(config.root)


@pytest.mark.parametrize('mutation', ['unchanged_number', 'change_unaffected_claim'])
def test_bad_local_correction_is_terminal_and_does_not_open_sibling_circuit(native_science, mutation):
    record, draft, config, analysis = native_science
    original = deepcopy(analysis)
    original['paragraphs'][1]['claims'][0]['text'] += '收益99%。'
    frozen = [candidate(record)]
    def run():
        with Execution(config.root / 'issue', 'bad-science-repair', frozen, config, protocol='offline-local-repair') as active:
            analyze_papers(config, [record], [draft], execution=active)
            assert active.snapshot()['writer_circuit'] is None
    with pytest.raises(PendingResponse): run()
    import_response(config.root, pending(config.root)[0]['job_id'], original, 'scientific-author')
    with pytest.raises(PendingResponse): run()
    corrected = deepcopy(original if mutation == 'unchanged_number' else analysis)
    if mutation == 'change_unaffected_claim': corrected['paragraphs'][0]['claims'][0]['text'] += '额外内容。'
    import_response(config.root, pending(config.root)[0]['job_id'], corrected, 'scientific-repair-author')
    run()
    state = record.reading['scientific_analysis']
    assert state['status'] == 'failed' and state['completion_scope'] == 'core_only'
    assert state['diagnostics'][0]['code'] == ('missing_numeric_tokens' if mutation == 'unchanged_number' else 'local_repair_scope_changed')
    assert not scientific_analysis_valid(record, draft)
    before = len(list((config.root / 'data/writer-queue').glob('*.job.json')))
    run()
    assert len(list((config.root / 'data/writer-queue').glob('*.job.json'))) == before


def test_rejected_second_packet_never_promotes_partial_evidence(multipage):
    record, draft, config = multipage
    frozen = [candidate(record)]
    def run():
        with Execution(config.root / 'issue', 'rejected-pack', frozen, config, protocol='offline-packs') as active:
            result = review_native_claims(draft, record, invoke(config), 45, execution=active)
            assert active.snapshot()['writer_circuit'] is None
            return result
    original = deepcopy(draft.to_dict())
    with pytest.raises(PendingResponse): run()
    first = pending(config.root)[0]
    import_response(config.root, first['job_id'], response_for_review(first), 'independent-reviewer')
    for _ in range(2):
        with pytest.raises(PendingResponse): run()
        assert draft.to_dict() == original
    second = pending(config.root)[0]
    response = response_for_review(second)
    response['checks'][0].update(supported=False, reason='Original pixels do not support this claim')
    import_response(config.root, second['job_id'], response, 'independent-reviewer')
    result = run()
    assert result['status'] == 'rejected'
    assert not native_claim_support_valid(record, draft)
    assert draft.verification['semantic_support'] == 'review_failed'


def test_multipack_deadline_exhaustion_admits_no_overflow(multipage):
    record, draft, config = multipage
    frozen, clock = [candidate(record)], StageClock()
    def factory():
        return Execution(config.root / 'issue', 'pack-deadline', frozen, config, protocol='offline-packs', clock=clock)
    with factory() as active:
        with pytest.raises(PendingResponse):
            review_native_claims(draft, record, invoke(config), 45, execution=active)
    first = pending(config.root)[0]
    import_response(config.root, first['job_id'], response_for_review(first), 'independent-reviewer')
    calls = []
    def charged(prompt, timeout, **kwargs):
        calls.append(prompt)
        result = invoke(config)(prompt, timeout, **kwargs)
        clock.advance(65)
        return result
    with factory() as active:
        result = review_native_claims(draft, record, charged, 45, execution=active)
        state = active.snapshot()
        assert result['status'] == 'failed' and result['error'] == 'BudgetExhausted'
        assert state['writer_circuit'] is None and state['pools']['primary_writer']['remaining'] == 0
        assert all(op['identity'][2] != 'semantic_overflow' for op in state['operations'].values())
    assert len(calls) == 1 and not pending(config.root)
    assert not native_claim_support_valid(record, draft)
