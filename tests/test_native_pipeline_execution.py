"""End-to-end offline parent queue fixture; counts are NOT model throughput.

Real PDF extraction, native grouped reading, selection, drafting, independent
CORE and scientific review use the same finite Execution and actual queue files.
Only fixture JSON answers are imported; no model or network transport is called.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

import pytest

from daily_agent.batch_execution import Execution, candidate, existing_review_validator
from daily_agent.deferred_review_cache import _supported, _assets
from daily_agent.editorial import PAPER_FIELDS, draft_report_items, review_draft
from daily_agent.paper_document import extract_document
from daily_agent.parent_writer import PendingResponse, import_response, pending
from daily_agent.reading import compact
from daily_agent.reading_batches import text_sha256
from daily_agent.scientific_analysis import WRITER_PROMPT, analyze_papers, scientific_analysis_valid
from daily_agent.source_evidence_policy import NATIVE, native_quality, native_text_complete
from test_native_claim_pixel_review import example, response_for_selection, response_for_review
from test_native_scientific_pixels import review_response as scientific_response


@pytest.fixture
def pipeline_input(example):
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
    record.reading = {}
    assert doc['document_kind'] == 'full_text'
    for ref in draft.claim_evidence:
        chunk = next(c for c in doc['chunks'] if compact(ref['quote']) in compact(c['text']))
        ref['chunk_id'] = chunk['id']
        ref['quote'] = next(line for line in chunk['text'].splitlines() if compact(ref['quote']) in compact(line))
    fields = {field: 'not_stated' for field in PAPER_FIELDS}
    fields.update(draft.draft_fields, method_steps=['not_stated'],
                  problem='论文比较模拟策略在基准问题中的表现。',
                  method='方法采用自适应策略更新。',
                  key_result='模拟成功率比较中，Ours 为81.2，Baseline 为72.0。')
    draft.draft_fields = fields
    draft.verification = {}
    config.sources['reading'].update(source_evidence_policy=NATIVE, run_budget_seconds=30,
        visual_budget_seconds=20, fidelity_budget_seconds=40, max_chunks_per_paper=20,
        fidelity_enabled=True, visual_enabled=True, concurrent_reads=1)
    config.sources['llm_writer'].update(run_budget_seconds=120, timeout_seconds=45)
    config.delivery = {}
    analysis = {'schema_version': 1, 'paragraphs': [
        {'id': section, 'claims': [{'id': section, 'kind': 'interpretation',
            'text': '模拟比较提供策略更新的受限证据，尚不能单独定位机制。', 'facets': tags,
            'evidence': [{k: v for k, v in ref.items() if k != 'field'} for ref in draft.claim_evidence]}]}
        for section, tags in [('insight', ['valuable_insight', 'precise_problem']),
                              ('explanation', ['bottleneck', 'minimal_idea', 'complexity']),
                              ('argument', ['narrative', 'decisive_evidence', 'unresolved'])]]}
    return record, draft, config, analysis


def _offline_answer(job, draft, analysis):
    prompt = job['prompt']
    if prompt.startswith('阅读论文的一个原文块批次'):
        value = json.loads(prompt.split('输入：\n', 1)[1])
        return ({'contract': value['contract'], 'results': [
            {'chunk_id': c['id'], 'text_sha256': text_sha256(c['text']),
             'note': {'chunk_id': c['id'], 'summary': '离线测试已读取这一完整原文块。',
                      'quotes': [c['text']], 'conditions': 'Simulation only', 'evidence_kind': 'simulation'}}
            for c in value['chunks']]}, 'offline-reader', 'native_reading')
    if job['stage'] == 'visual_selection':
        return response_for_selection(job), 'offline-selector', 'visual_selection'
    if prompt.startswith('你是 Daily_Agent 编辑部写手'):
        return ([{'key': draft.key, 'draft_fields': draft.draft_fields, 'claim_evidence': draft.claim_evidence,
                  'evidence_used': ['原始PDF完整分块', '原文结果表及图'], 'writer_notes': '离线固定回答，仅检验协议。'}],
                'offline-core-writer', 'core_writer')
    if prompt.startswith('独立证据核验员'):
        return response_for_review(job), 'offline-core-reviewer', 'core_pixel_review'
    if prompt.startswith(WRITER_PROMPT):
        return analysis, 'offline-science-writer', 'scientific_writer'
    if 'claim_bindings' in prompt and job['stage'] == 'review':
        return scientific_response(job), 'offline-science-reviewer', 'scientific_pixel_review'
    raise AssertionError('Unexpected or unbounded pipeline job: ' + prompt[:100])


def test_native_pipeline_exact_counts_and_zero_new_jobs_on_original_resume(pipeline_input):
    original, offline_draft, config, analysis = pipeline_input
    frozen = [candidate(original)]
    def execution():
        return Execution(config.root / 'issue', 'original-native-pipeline-offline', frozen, config,
                         protocol='native-pipeline-offline-v1')
    def run():
        record = deepcopy(original)
        with execution() as active:
            drafts = draft_report_items(config, [record], execution=active)
            assert len(drafts) == 1
            analyze_papers(config, [record], drafts, execution=active)
        return record, drafts[0]
    calls = Counter()
    # This finite bound is a test guard, not an operational polling condition.
    for _ in range(12):
        try:
            record, draft = run()
            break
        except PendingResponse:
            jobs = pending(config.root)
            assert jobs
            for job in jobs:
                answer, worker, label = _offline_answer(job, offline_draft, analysis)
                calls[label] += 1
                import_response(config.root, job['job_id'], answer, worker)
    else:
        pytest.fail('Offline fixture did not reach its exact finite terminal state')
    assert native_text_complete(record)
    assert native_quality(record, draft)
    assert scientific_analysis_valid(record, draft)
    assert _supported(record, draft, config=config)
    # This intentionally tiny fixture verifies stage/transport termination,
    # not publication quality or completion rights. Editorial quality remains.
    editorial = review_draft(config, [draft], use_llm=False)[0]
    assert editorial.verdict == 'FAIL'
    envelope = {'material': record.key, 'record': record.to_dict(), 'draft': draft.to_dict(),
                'review': {**editorial.to_dict(), 'verdict': 'PASS'},
                'asset_hashes': _assets(config, record)}
    assert not existing_review_validator(config)(envelope)
    assert calls == {'native_reading': 1, 'visual_selection': 1, 'core_writer': 1,
                     'core_pixel_review': 1, 'scientific_writer': 1, 'scientific_pixel_review': 1}
    assert not record.reading.get('visual', {}).get('strict_fidelity')
    queue = config.root / 'data/writer-queue'
    first_jobs = {p.name for p in queue.glob('*.job.json')}
    assert len(first_jobs) == 6
    with execution() as active:
        first = active.snapshot()
    operations = [op['identity'] for op in first['operations'].values()]
    native = [op for op in operations if op[1] == 'native']
    primary = [op for op in operations if op[1] == 'primary_writer']
    assert len(native) == len(original.paper_document['chunks']) == 4
    assert {op[2] for op in primary} == {'native_visual_selection', 'draft', 'semantic', 'scientific_writer', 'scientific_review'}
    assert len(operations) == 9
    assert not any(op[1] in {'visual', 'fidelity', 'repaired'} for op in operations)
    assert first['pools']['primary_writer']['limit'] == 120
    assert first['pools']['native']['limit'] == 30
    assert first['writer_circuit'] is None and not first['reservations']
    # Repeat from the original frozen input, same ledger and cache namespace.
    resumed_record, resumed_draft = run()
    assert _supported(resumed_record, resumed_draft, config=config)
    assert scientific_analysis_valid(resumed_record, resumed_draft)
    assert {p.name for p in queue.glob('*.job.json')} == first_jobs
    assert not pending(config.root)
    with execution() as active:
        second = active.snapshot()
    assert second['operations'] == first['operations']
    assert second['pools'] == first['pools']  # cache reuse spends no stage budget
