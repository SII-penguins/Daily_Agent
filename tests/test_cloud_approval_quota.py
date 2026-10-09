"""Offline cloud eligibility tests; no live fetching, model, or delivery."""
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from daily_agent.config import load_config
from daily_agent.editorial import approve_publication
from daily_agent.models import MaterialRecord, EditorialDraft, EditorialReview
from daily_agent.cloud_workflow import _qualifying_rows


def config(tmp_path, cloud=True):
    cfg = load_config(Path(__file__).resolve().parents[1])
    object.__setattr__(cfg, 'root', tmp_path)
    cfg.delivery['cloud'] = {'profile': 'test'} if cloud else {}
    cfg.quota.update(max_items=1, paper_target=1, github_target=0)
    cfg.sources['selection']['balance_quantum_directions'] = False
    return cfg


def candidate(key, *, kind='paper', status='located', complete=True):
    record = MaterialRecord(key=key, source='github' if kind == 'repo' else 'arxiv',
                            item_type=kind, title=key, url=f'https://example.org/{key}')
    record.paper_document = {'document_kind': 'full_text', 'source_type': 'html'}
    record.reading = {'complete': complete}
    verification = {'status': status, 'semantic_support': 'model_checked',
                    'valid_fields': ['problem', 'method']}
    draft = EditorialDraft(key, kind, key, {'problem': 'Supported problem', 'method': 'Supported method'},
                           verification=verification if kind == 'paper' else {})
    return record, draft, EditorialReview(key, 'PASS')


def approve(cfg, candidates):
    return approve_publication(cfg, *[list(values) for values in zip(*candidates)])


@pytest.mark.parametrize("balanced", [False, True])
def test_cloud_filters_before_quota_and_matches_final_handoff(tmp_path, balanced):
    cfg = config(tmp_path)
    cfg.sources["selection"]["balance_quantum_directions"] = balanced
    # An incomplete located card sorted before the good card used to consume
    # max_items. A limited PASS must also remain non-deliverable.
    candidates = [candidate('incomplete', complete=False), candidate('limited', status='limited'),
                  candidate('qualified')]
    approved = approve(cfg, candidates)
    assert [item.key for item in approved] == ['qualified']
    rows, excluded = _qualifying_rows([item.to_dict() for item in approved])
    assert [row['key'] for row in rows] == ['qualified'] and excluded == []
    assert candidates[1][1].verification['status'] == 'limited'
    assert candidates[1][2].verdict == 'PASS'  # retained review, not promoted


@pytest.mark.parametrize('failure', ['incomplete', 'limited', 'unreviewed', 'fidelity'])
def test_cloud_independent_evidence_gates_still_apply(tmp_path, failure):
    cfg = config(tmp_path)
    record, draft, review = candidate('paper')
    if failure == 'incomplete':
        record.reading['complete'] = False
    elif failure == 'limited':
        draft.verification['status'] = 'limited'
    elif failure == 'unreviewed':
        draft.verification['semantic_support'] = 'not_checked'
    else:
        record.paper_document['source_type'] = 'pdf'
        record.reading['visual'] = {'strict_fidelity': True, 'fidelity': {'passed': False}}
    assert approve(cfg, [(record, draft, review)]) == []


def test_legacy_limited_pass_and_cloud_repos_keep_existing_policy(tmp_path):
    cfg = config(tmp_path, cloud=False)
    assert approve(cfg, [candidate('limited', status='limited')])[0].key == 'limited'
    cfg.delivery['cloud'] = {'profile': 'test'}
    assert approve(cfg, [candidate('repo', kind='repo')])[0].key == 'repo'


@pytest.mark.parametrize("qualified_count", [0, 1])
def test_cloud_pipeline_refills_limited_without_expanding_batch_budget(tmp_path, monkeypatch, qualified_count):
    import daily_agent.pipeline as pipeline
    import daily_agent.author_context as authors
    import daily_agent.scientific_analysis as analysis
    cfg = config(tmp_path)
    cfg.quota.update(max_items=10, paper_target=8, github_target=2)
    cfg.sources['selection'].update(editorial_batch_size=8, max_review_batches=6)
    candidates = [candidate(f'paper:{i}', status='located' if qualified_count and i == 6 else 'limited') for i in range(60)]
    candidates += [candidate(f'repo:{i}', kind='repo') for i in range(20)]
    by_key = {record.key: (record, draft, review) for record, draft, review in candidates}
    library = {key: values[0] for key, values in by_key.items()}
    monkeypatch.setattr(pipeline, 'load_config', lambda root: cfg)
    monkeypatch.setattr(pipeline, '_fetch_windowed_sources', lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, 'load_material_library', lambda *a: dict(library))
    monkeypatch.setattr(pipeline, '_build_shortlist_from_library', lambda cfg, remaining, day: list(remaining.values()))
    for name in ('enrich_open_access_links', 'enrich_unpaywall_links', 'enrich_paper_texts', 'enrich_citation_contexts', 'cache_selected_pdfs'):
        monkeypatch.setattr(pipeline, name, lambda values, *a: values)
    monkeypatch.setattr(authors, 'enrich_author_contexts', lambda values, *a: values)
    monkeypatch.setattr(analysis, 'analyze_papers', lambda *a, **kw: None)
    batches = []
    def draft(cfg, records, **kwargs):
        batches.append([r.key for r in records])
        return [by_key[r.key][1] for r in records]
    monkeypatch.setattr(pipeline, 'draft_report_items', draft)
    def review(cfg, drafts, **kwargs):
        return [EditorialReview(d.key, 'FAIL' if d.item_type == 'repo' else 'PASS') for d in drafts]
    monkeypatch.setattr(pipeline, 'review_draft', review)
    before = deepcopy((cfg.sources, cfg.quota, cfg.delivery))
    result = pipeline.run_pipeline(tmp_path, date(2026, 10, 8), use_llm=False)
    assert len(batches) == 6 and all(len(batch) == 8 for batch in batches)
    assert all(sum(key.startswith('paper:') for key in batch) == 6 for batch in batches)
    assert len({key for batch in batches for key in batch}) == 48
    assert all(by_key[key][1].verification.get('status') != 'located' for key in batches[0])
    assert len(result.items) == qualified_count
    if qualified_count:
        assert result.items[0].key == 'paper:6' and 'paper:6' in batches[1]
    assert result.status.fallback == ('论文池补选后仍不足目标；保留证据审核门槛'
                                      if qualified_count else '编辑部未批准任何候选内容')
    rows, excluded = _qualifying_rows([item.to_dict() for item in result.items])
    assert len(rows) == qualified_count and not excluded
    assert (cfg.sources, cfg.quota, cfg.delivery) == before
