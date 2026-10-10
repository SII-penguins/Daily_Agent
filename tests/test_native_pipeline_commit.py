"""Positive native pipeline completion through real unchanged acceptance gates.

Synthetic source and offline model-response fixtures exercise software contracts,
not scientific/model quality. Nothing is published or written to a real archive.
The separate shallow pipeline fixture remains the editorial FAIL control.
"""
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from daily_agent.batch_execution import Execution, candidate, existing_review_validator
from daily_agent.deferred_review_cache import _assets, _supported
from daily_agent.editorial import draft_report_items, review_draft
from daily_agent.paper_document import extract_document
from daily_agent.parent_writer import PendingResponse, import_response, pending
from daily_agent.reading import CORE, compact
from daily_agent.scientific_analysis import analyze_papers, scientific_analysis_valid
from daily_agent.source_evidence_policy import native_quality, native_text_complete
from daily_agent.workflow_state import StateCorrupt
from test_native_claim_pixel_review import example
from test_native_pipeline_execution import pipeline_input, _offline_answer


@pytest.fixture
def commit_input(pipeline_input):
    record, draft, config, _ = pipeline_input
    import fitz
    sentences = {
        'problem': 'A fixed update schedule cannot adapt to changing training feedback under a fixed training budget.',
        'method': 'The method estimates recent feedback and adapts the policy update frequency under the same training budget.',
        'why_it_works': 'Adaptive scheduling aims to avoid redundant updates when recent feedback changes little; this is a proposed rationale, not a causal proof.',
        'novelty_or_difference': 'Unlike the fixed schedule baseline, the method adjusts update frequency while holding the training budget fixed.',
        'technical_route': 'The procedure collects training feedback, computes a recent estimate, adjusts update frequency, and compares simulated success rates.',
        'key_result': 'Table 1 reports Ours 81.2% and Baseline 72.0% as simulated success rates under the same task and training budget.',
        'limitations': 'Evidence is restricted to this simulated benchmark; no hardware experiment or separate component ablation is reported.',
        'possible_use_or_impact': 'Transfer to another task is only a proposed hypothesis and requires a new baseline-controlled evaluation.',
        'narrative': 'The study introduces a benchmark problem, describes the update rule, and then presents the result comparison.',
    }
    path = Path(record.paper_document['source_pdf_path'])
    with fitz.open(path) as pdf:
        page = pdf.new_page(width=595, height=842)
        text = ('Methods\n' + '\n'.join(sentences[k] for k in ('problem', 'method', 'why_it_works',
                'novelty_or_difference', 'technical_route')) + '\nResults\n' + sentences['key_result'] +
                '\nDiscussion\n' + '\n'.join(sentences[k] for k in ('limitations', 'possible_use_or_impact', 'narrative')))
        assert page.insert_textbox(fitz.Rect(25, 25, 570, 815), text, fontsize=10) >= 0
        pdf.saveIncr()
    doc = extract_document(path.read_bytes(), record, record.paper_document['source_url'],
                           {'min_body_chars': 100, 'ocr_enabled': False, 'chunk_chars': 2000})
    doc['source_pdf_path'] = str(path)
    record.paper_document = doc
    record.reading = {}
    assert doc['document_kind'] == 'full_text'
    fields = {
        'problem': '论文研究固定训练预算下策略更新的适应性问题：固定更新安排不能随训练反馈变化，因而需要检验动态调整是否有价值。',
        'method': '方法先估计近期训练反馈，再据此调整策略更新频率；与固定安排基线比较时保持训练预算一致，避免混入预算差异。',
        'why_it_works': '作者提出的解释是，近期反馈变化较小时减少冗余更新可能有益；这只是方法的设计依据，文中结果并没有单独证明该因果机制。',
        'novelty_or_difference': '相对固定更新安排，区别在于根据近期反馈调整更新频率，同时保持训练预算一致；新意限定于这一具体基线比较。',
        'technical_route': '技术流程依次收集训练反馈、计算近期估计、调整策略更新频率，再在模拟基准上比较成功率，明确保留相同训练预算。',
        'method_steps': ['收集训练反馈并计算近期估计，用该估计调整策略更新频率。', '在相同训练预算的模拟基准上比较成功率。'],
        'key_result': '相同任务与训练预算的模拟比较中，Ours 的成功率为81.2%，固定安排 Baseline 为72.0%；这一结果只支持该模拟基准上的比较。',
        'limitations': '证据范围局限于这一模拟基准，来源明确未报告硬件实验及独立组件消融；因此不能据此确定单一组件的因果贡献。',
        'possible_use_or_impact': '编辑迁移设想是把这种根据反馈调整更新频率的思路用于另一任务，但必须重新进行有基线控制的评估，不能把原有模拟收益直接外推。',
        'evidence_from_source': sentences['key_result'], 'confidence': 'medium',
    }
    claims = []
    for field in CORE:
        quote = sentences['technical_route' if field == 'method_steps' else field]
        chunk = next(c for c in doc['chunks'] if compact(quote) in compact(c['text']))
        # Preserve actual extracted line wrapping in the evidence quote.
        claims.append({'field': field, 'chunk_id': chunk['id'], 'quote': quote,
                       'conditions': 'Same simulated task and fixed training budget; fixed schedule baseline',
                       'evidence_kind': 'simulation'})
    draft.draft_fields = fields
    draft.claim_evidence = claims
    draft.evidence_used = [sentences['method'], sentences['key_result']]
    draft.verification = {}
    refs = [{k: v for k, v in ref.items() if k != 'field'} for ref in claims]
    paragraphs = [
        ('insight', ['valuable_insight', 'precise_problem'], '有限训练预算下，固定更新安排难以响应反馈变化；一个值得检验的思想是让更新频率随近期反馈调整。'),
        ('explanation', ['bottleneck', 'minimal_idea', 'complexity'], '这种设计以近期反馈估计决定更新频率，意在减少变化较小时的冗余更新；组件消融缺失，尚不能确认该额外估计的独立必要性。'),
        ('argument', ['narrative', 'decisive_evidence', 'unresolved'], '论文先提出基准问题，再说明更新规则，最后比较模拟成功率；相同预算的比较提供受限证据，迁移到另一任务仍需要新的基线控制实验。'),
    ]
    analysis = {'schema_version': 1, 'paragraphs': [
        {'id': key, 'claims': [{'id': key, 'kind': 'interpretation', 'text': text, 'facets': facets,
                              'evidence': deepcopy(refs)}]} for key, facets, text in paragraphs]}
    # The chronology statement also has an exact source premise.
    chunk = next(c for c in doc['chunks'] if compact(sentences['narrative']) in compact(c['text']))
    analysis['paragraphs'][-1]['claims'][0]['evidence'].append(
        {'chunk_id': chunk['id'], 'quote': sentences['narrative'], 'conditions': 'Paper argument order only', 'evidence_kind': 'simulation'})
    return record, draft, config, analysis


def test_complete_native_pipeline_passes_real_editorial_commit_and_replay(commit_input):
    original, fixture_draft, config, analysis = commit_input
    frozen = [candidate(original)]
    validator = existing_review_validator(config)
    def execution():
        return Execution(config.root / 'synthetic-issue', 'native-positive-offline', frozen, config,
                         protocol='native-positive-pipeline-offline-v1')
    def run():
        record = deepcopy(original)
        with execution() as active:
            drafts = draft_report_items(config, [record], execution=active)
            assert len(drafts) == 1
            analyze_papers(config, [record], drafts, execution=active)
        return record, drafts[0]
    calls = Counter()
    for _ in range(15):
        try:
            record, draft = run()
            break
        except PendingResponse:
            for job in pending(config.root):
                answer, worker, label = _offline_answer(job, fixture_draft, analysis)
                if label == 'native_reading':
                    # Every chunk is read; notes keep bounded exact excerpts.
                    for row in answer['results']:
                        text = row['note']['quotes'][0]
                        row['note']['quotes'] = [text[:min(len(text), 500)]]
                import_response(config.root, job['job_id'], answer, worker)
                calls[label] += 1
    else:
        pytest.fail('Positive fixture did not terminate within its finite stage topology')
    assert native_text_complete(record) and native_quality(record, draft), draft.verification
    assert scientific_analysis_valid(record, draft) and _supported(record, draft, config=config)
    review = review_draft(config, [draft], use_llm=False)[0]
    assert review.verdict == 'PASS', review.issues
    assert not review.issues
    assets = _assets(config, record)
    assert assets
    envelope = {'material': record.key, 'record': record.to_dict(), 'draft': draft.to_dict(),
                'review': review.to_dict(), 'asset_hashes': assets}
    assert validator(envelope) is True
    with execution() as active:
        assert active.completed(record.key, validator=validator) is None
        sha = active.prepare_completion(record.key, record=record.to_dict(), draft=draft.to_dict(),
            review=review.to_dict(), science=record.reading['scientific_analysis'],
            evidence={'reading': record.reading}, asset_hashes=assets)
        assert active.completed(record.key, validator=validator) is None  # preparation alone grants nothing
        active.commit_completion(record.key, sha, validator=validator)
        completed = active.completed(record.key, validator=validator)
        assert completed['draft'] == draft.to_dict()
        first = active.snapshot()
    assert calls == {'native_reading': 2, 'visual_selection': 1, 'core_writer': 1,
                     'core_pixel_review': 1, 'scientific_writer': 1, 'scientific_pixel_review': 1}
    assert len(first['operations']) == len(original.paper_document['chunks']) + 5
    assert not any(op['identity'][1] in {'visual', 'fidelity', 'repaired'} for op in first['operations'].values())
    jobs = {p.name for p in (config.root / 'data/writer-queue').glob('*.job.json')}
    assert len(jobs) == 7 and not pending(config.root)
    resumed_record, resumed_draft = run()
    assert _supported(resumed_record, resumed_draft, config=config)
    assert review_draft(config, [resumed_draft], use_llm=False)[0].verdict == 'PASS'
    with execution() as active:
        assert active.completed(record.key, validator=validator) == completed
        second = active.snapshot()
    assert first['completed'] == second['completed']
    assert first['operations'] == second['operations'] and first['pools'] == second['pools']
    assert {p.name for p in (config.root / 'data/writer-queue').glob('*.job.json')} == jobs
    assert not record.published_dates and not resumed_record.published_dates
    assert not (config.root / 'data/archive').exists()
    # Committed evidence still revalidates source bytes, including after reload.
    Path(record.paper_document['source_pdf_path']).write_bytes(b'%PDF modified after the synthetic commit')
    with pytest.raises(StateCorrupt):
        with execution() as active:
            active.completed(record.key, validator=validator)
