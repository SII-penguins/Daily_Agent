from datetime import date

import httpx

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def _response(url, payload):
    return httpx.Response(200, request=httpx.Request("GET", url), json=payload)


def test_oa_resolver_adds_openalex_pdf_and_evidence(tmp_path, monkeypatch):
    from daily_agent.connectors import oa_resolver
    from daily_agent.connectors.oa_resolver import enrich_open_access_links

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["oa_resolver"] = {"enabled": True, "max_papers_per_run": 5, "timeout_seconds": 30}
    material = MaterialRecord(
        key="doi:10.1234/oa",
        source="crossref",
        item_type="paper",
        title="OA resolver paper",
        url="https://doi.org/10.1234/oa",
        doi="10.1234/oa",
    )
    seen_urls: list[str] = []

    def handler(url, **kwargs):
        seen_urls.append(url)
        return _response(
            url,
            {
                "id": "https://openalex.org/W-OA",
                "title": "OA resolver paper",
                "primary_location": {
                    "landing_page_url": "https://publisher.test/oa",
                    "pdf_url": "https://publisher.test/oa.pdf",
                    "source": {"display_name": "OA Test Journal"},
                },
                "open_access": {"oa_url": "https://publisher.test/oa.pdf", "is_oa": True},
            },
        )

    monkeypatch.setattr(oa_resolver.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))

    enriched = enrich_open_access_links([material], config)

    assert seen_urls == ["https://api.openalex.org/works/doi:10.1234/oa"]
    assert enriched[0].pdf_url == "https://publisher.test/oa.pdf"
    assert enriched[0].raw["openalex_oa_resolver"]["status"] == "resolved"
    assert enriched[0].raw["open_access_url"] == "https://publisher.test/oa.pdf"
    assert enriched[0].raw["openalex_url"] == "https://openalex.org/W-OA"
    assert enriched[0].raw["venue"] == "OA Test Journal"
    assert enriched[0].evidence["sources"]["openalex"]["pdf_url"] == "https://publisher.test/oa.pdf"
    assert enriched[0].evidence["sources"]["openalex"]["landing_url"] == "https://publisher.test/oa"


def test_oa_resolver_adds_landing_page_for_html_fulltext_fallback(tmp_path, monkeypatch):
    from daily_agent.connectors import oa_resolver
    from daily_agent.connectors.oa_resolver import enrich_open_access_links
    from daily_agent.connectors.paper_text import _candidate_html_urls

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["oa_resolver"] = {"enabled": True, "max_papers_per_run": 5, "timeout_seconds": 30}
    material = MaterialRecord(
        key="doi:10.1234/html-oa",
        source="crossref",
        item_type="paper",
        title="OA landing paper",
        url="https://doi.org/10.1234/html-oa",
        doi="10.1234/html-oa",
    )

    def handler(url, **kwargs):
        return _response(
            url,
            {
                "id": "https://openalex.org/W-HTML",
                "primary_location": {
                    "landing_page_url": "https://publisher.test/html-oa",
                    "source": {"display_name": "OA Landing Journal"},
                },
                "open_access": {"oa_url": "https://publisher.test/html-oa", "is_oa": True},
            },
        )

    monkeypatch.setattr(oa_resolver.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))

    enriched = enrich_open_access_links([material], config)

    assert enriched[0].pdf_url is None
    assert enriched[0].raw["open_access_url"] == "https://publisher.test/html-oa"
    assert "https://publisher.test/html-oa" in _candidate_html_urls(enriched[0])


def test_pipeline_resolves_oa_links_before_fulltext_enrichment(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(
        key="doi:10.1234/pipeline-oa",
        source="crossref",
        item_type="paper",
        title="Pipeline OA paper",
        url="https://doi.org/10.1234/pipeline-oa",
        doi="10.1234/pipeline-oa",
        abstract="This paper studies quantum compiler agents. We propose OA-guided full text retrieval. It works by resolving accessible PDFs. Results improve coverage by 20%. Limitations include OpenAlex coverage.",
        score=90,
        tags=["quantum"],
        quota_group="quantum",
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "problem",
            "method": "method",
            "why_it_works": "why",
            "key_result": "result",
            "limitations": "limitations",
        },
        material=material,
    )

    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_citation_discovery",
        "fetch_google_scholar",
        "fetch_crossref",
        "fetch_core",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "load_config", lambda root=None: config)
    monkeypatch.setattr(pipeline, "load_material_library", lambda config_arg: {material.key: material})
    monkeypatch.setattr(pipeline, "select_library_candidates", lambda config_arg, library, run_date: [material])
    monkeypatch.setattr(pipeline, "_build_shortlist_from_library", lambda config_arg, library, run_date: [material])

    def fake_resolve(records, config_arg):
        records[0].pdf_url = "https://publisher.test/pipeline-oa.pdf"
        return records

    def fake_fulltext(records, config_arg):
        assert records[0].pdf_url == "https://publisher.test/pipeline-oa.pdf"
        records[0].paper_text_excerpt = "Full text came from the OA PDF."
        return records

    monkeypatch.setattr(pipeline, "enrich_open_access_links", fake_resolve)
    monkeypatch.setattr(pipeline, "enrich_paper_texts", fake_fulltext)
    monkeypatch.setattr(pipeline, "enrich_citation_contexts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "draft_report_items", lambda config_arg, shortlist, use_llm=True: [])
    monkeypatch.setattr(pipeline, "review_draft", lambda config_arg, drafts, use_llm=True: [])
    monkeypatch.setattr(pipeline, "approve_publication", lambda config_arg, shortlist, drafts, reviews: [approved])
    monkeypatch.setattr(pipeline, "cache_selected_pdfs", lambda items, config_arg, run_date: items)

    result = pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 7, 9), dry_run=True, use_llm=False)

    assert result.items[0].material.pdf_url == "https://publisher.test/pipeline-oa.pdf"


def test_quality_check_requires_oa_resolver_full_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["oa_resolver"] = {
        "enabled": False,
        "max_papers_per_run": 5,
        "timeout_seconds": 10,
        "run_budget_seconds": 0,
    }

    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert by_key["oa_resolver"].status == "disabled"
    assert "enabled=false" in by_key["oa_resolver"].detail
    assert "max_papers_per_run=5" in by_key["oa_resolver"].detail
    assert "run_budget_seconds=0" in by_key["oa_resolver"].detail
