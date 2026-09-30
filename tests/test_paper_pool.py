from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.models import DigestItem, MaterialRecord
from daily_agent.pipeline import _prepare_selected_items
from daily_agent.storage import select_library_candidates, upsert_materials


def paper(i):
    return DigestItem(id=str(i), source='arxiv', arxiv_id=f'2609.{i:05}',
                      item_type='paper', title=f'Quantum circuit optimization method {i}',
                      abstract='Hardware-aware quantum circuit synthesis and quantum compilation.',
                      url=f'https://arxiv.org/abs/2609.{i:05}', published_at='2026-09-20T00:00:00Z')


def test_overflow_survives_in_pool_for_next_day(tmp_path):
    cfg = load_config()
    object.__setattr__(cfg, 'root', tmp_path)
    items = _prepare_selected_items([paper(i) for i in range(50)], cfg, {},
                                   datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert len(items) == 50
    library = upsert_materials(cfg, items, date(2026, 9, 30))
    assert len(select_library_candidates(cfg, library, date(2026, 10, 1))) == 50
    assert all(not r.published_dates for r in library.values())


def test_pool_refreshes_age_and_suppresses_arxiv_doi_duplicate():
    cfg = load_config()
    record = MaterialRecord.from_item(paper(1))
    record.score = 999
    other = MaterialRecord.from_item(paper(2))
    other.key = 'doi:10.48550/arxiv.2609.00001'
    other.doi = '10.48550/arxiv.2609.00001'
    other.source = 'crossref'
    library = {r.key:r for r in [record, other]}
    candidates = select_library_candidates(cfg, library, date(2026, 9, 30))
    assert len(candidates) == 1
    first = record.score_breakdown['freshness']
    select_library_candidates(cfg, library, date(2026, 11, 30))
    assert record.score_breakdown['freshness'] < first


def test_pipeline_refills_after_review_rejection(tmp_path, monkeypatch):
    from daily_agent import pipeline
    from daily_agent.models import ApprovedItem
    cfg = load_config()
    object.__setattr__(cfg, 'root', tmp_path)
    cfg.quota.update(max_items=2, paper_target=2, paper_review_target=2,
                     paper_review_multiplier=1, github_target=0)
    cfg.sources['arxiv']['fallback_windows_days'] = []
    cfg.sources['pdf_cache'] = {'enabled': False}
    monkeypatch.setattr(pipeline, 'load_config', lambda root: cfg)
    monkeypatch.setattr(pipeline, '_fetch_windowed_sources', lambda *args: [paper(i) for i in range(5)])
    for name in ['enrich_open_access_links', 'enrich_unpaywall_links', 'enrich_paper_texts', 'enrich_citation_contexts']:
        monkeypatch.setattr(pipeline, name, lambda records, config: records)
    calls = []
    def draft(config, records, use_llm):
        calls.append([r.key for r in records])
        return []
    monkeypatch.setattr(pipeline, 'draft_report_items', draft)
    monkeypatch.setattr(pipeline, 'review_draft', lambda *args, **kwargs: [])
    def approve(config, records, drafts, reviews):
        return [ApprovedItem(key=r.key,item_type='paper',title=r.title,source=r.source,
                             url=r.url,final_fields={},material=r,approval_notes='test')
                for r in records[2:4]]
    monkeypatch.setattr(pipeline, 'approve_publication', approve)
    result = pipeline.run_pipeline(tmp_path, date(2026, 9, 30), use_llm=False)
    assert len(result.items) == 2
    assert len(calls) == 2
    assert not set(calls[0]) & set(calls[1])
