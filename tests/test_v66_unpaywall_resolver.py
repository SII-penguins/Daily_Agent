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


def test_unpaywall_resolver_adds_pdf_and_evidence(tmp_path, monkeypatch):
    from daily_agent.connectors import unpaywall
    from daily_agent.connectors.paper_text import _candidate_pdf_urls
    from daily_agent.connectors.unpaywall import enrich_unpaywall_links

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["unpaywall"] = {
        "enabled": True,
        "email_env": "UNPAYWALL_EMAIL",
        "max_papers_per_run": 5,
        "timeout_seconds": 15,
    }
    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.com")
    material = MaterialRecord(
        key="doi:10.1234/upw",
        source="crossref",
        item_type="paper",
        title="Unpaywall paper",
        url="https://doi.org/10.1234/upw",
        doi="10.1234/upw",
    )
    seen: list[tuple[str, dict]] = []

    def handler(url, **kwargs):
        seen.append((url, kwargs.get("params") or {}))
        return _response(
            url,
            {
                "doi": "10.1234/upw",
                "title": "Unpaywall paper",
                "is_oa": True,
                "best_oa_location": {
                    "url_for_pdf": "https://repository.test/upw.pdf",
                    "url_for_landing_page": "https://repository.test/upw",
                    "host_type": "repository",
                    "license": "cc-by",
                    "version": "publishedVersion",
                },
            },
        )

    monkeypatch.setattr(unpaywall.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))

    enriched = enrich_unpaywall_links([material], config)

    assert seen == [("https://api.unpaywall.org/v2/10.1234/upw", {"email": "researcher@example.com"})]
    assert enriched[0].pdf_url == "https://repository.test/upw.pdf"
    assert enriched[0].raw["unpaywall_resolver"]["status"] == "resolved"
    assert enriched[0].raw["open_access_url"] == "https://repository.test/upw.pdf"
    assert enriched[0].raw["unpaywall_url"] == "https://repository.test/upw"
    assert enriched[0].evidence["sources"]["unpaywall"]["pdf_url"] == "https://repository.test/upw.pdf"
    assert enriched[0].evidence["sources"]["unpaywall"]["landing_url"] == "https://repository.test/upw"
    assert "https://repository.test/upw.pdf" in _candidate_pdf_urls(enriched[0])


def test_unpaywall_resolver_adds_landing_page_for_html_fulltext_fallback(tmp_path, monkeypatch):
    from daily_agent.connectors import unpaywall
    from daily_agent.connectors.paper_text import _candidate_html_urls
    from daily_agent.connectors.unpaywall import enrich_unpaywall_links

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["unpaywall"] = {
        "enabled": True,
        "email_env": "UNPAYWALL_EMAIL",
        "max_papers_per_run": 5,
        "timeout_seconds": 15,
    }
    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.com")
    material = MaterialRecord(
        key="doi:10.1234/upw-html",
        source="crossref",
        item_type="paper",
        title="Unpaywall HTML paper",
        url="https://doi.org/10.1234/upw-html",
        doi="10.1234/upw-html",
    )

    def handler(url, **kwargs):
        return _response(
            url,
            {
                "doi": "10.1234/upw-html",
                "is_oa": True,
                "best_oa_location": {
                    "url_for_landing_page": "https://publisher.test/upw-html",
                    "host_type": "publisher",
                },
            },
        )

    monkeypatch.setattr(unpaywall.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))

    enriched = enrich_unpaywall_links([material], config)

    assert enriched[0].pdf_url is None
    assert enriched[0].raw["open_access_url"] == "https://publisher.test/upw-html"
    assert "https://publisher.test/upw-html" in _candidate_html_urls(enriched[0])


def test_unpaywall_resolver_reuses_persistent_cache_without_email_or_network(tmp_path, monkeypatch):
    from daily_agent.connectors import unpaywall
    from daily_agent.connectors.unpaywall import enrich_unpaywall_links

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["unpaywall"] = {
        "enabled": True,
        "email_env": "UNPAYWALL_EMAIL",
        "max_papers_per_run": 5,
        "timeout_seconds": 15,
        "cache_enabled": True,
        "cache_path": "data/cache/unpaywall.json",
        "cache_max_entries": 50_000,
    }
    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.com")
    first = MaterialRecord(
        key="doi:10.1234/cache-upw",
        source="crossref",
        item_type="paper",
        title="Cached Unpaywall paper",
        url="https://doi.org/10.1234/cache-upw",
        doi="10.1234/cache-upw",
    )
    calls = 0

    def handler(url, **kwargs):
        nonlocal calls
        calls += 1
        return _response(
            url,
            {
                "doi": "10.1234/cache-upw",
                "title": "Cached Unpaywall paper",
                "best_oa_location": {
                    "url_for_pdf": "https://repository.test/cache-upw.pdf",
                    "url_for_landing_page": "https://repository.test/cache-upw",
                },
            },
        )

    monkeypatch.setattr(unpaywall.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))
    enrich_unpaywall_links([first], config)

    assert calls == 1
    assert (tmp_path / "data" / "cache" / "unpaywall.json").exists()

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    second = MaterialRecord(
        key="doi:10.1234/cache-upw",
        source="crossref",
        item_type="paper",
        title="Cached Unpaywall paper",
        url="https://doi.org/10.1234/cache-upw",
        doi="10.1234/cache-upw",
    )
    monkeypatch.setattr(
        unpaywall.httpx,
        "Client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("network should not be used for cached DOI")),
    )

    enriched = enrich_unpaywall_links([second], config)

    assert enriched[0].pdf_url == "https://repository.test/cache-upw.pdf"
    assert enriched[0].raw["unpaywall_resolver"]["status"] == "cache_hit"
    assert enriched[0].raw["unpaywall_url"] == "https://repository.test/cache-upw"
    assert enriched[0].evidence["sources"]["unpaywall"]["pdf_url"] == "https://repository.test/cache-upw.pdf"


def test_pipeline_resolves_unpaywall_after_openalex_before_fulltext(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(
        key="doi:10.1234/pipeline-upw",
        source="crossref",
        item_type="paper",
        title="Pipeline Unpaywall paper",
        url="https://doi.org/10.1234/pipeline-upw",
        doi="10.1234/pipeline-upw",
        abstract="This paper studies quantum compiler agents. It proposes full text enrichment. Results improve coverage. Limitations include access.",
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
        final_fields={"problem": "p", "method": "m", "why_it_works": "w", "key_result": "r", "limitations": "l"},
        material=material,
    )
    order: list[str] = []

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
    monkeypatch.setattr(pipeline, "_build_shortlist_from_library", lambda config_arg, library, run_date: [material])
    monkeypatch.setattr(pipeline, "enrich_open_access_links", lambda records, config_arg: order.append("openalex") or records)

    def fake_unpaywall(records, config_arg):
        order.append("unpaywall")
        records[0].pdf_url = "https://repository.test/pipeline-upw.pdf"
        return records

    def fake_fulltext(records, config_arg):
        order.append("paper_text")
        assert records[0].pdf_url == "https://repository.test/pipeline-upw.pdf"
        return records

    monkeypatch.setattr(pipeline, "enrich_unpaywall_links", fake_unpaywall)
    monkeypatch.setattr(pipeline, "enrich_paper_texts", fake_fulltext)
    monkeypatch.setattr(pipeline, "enrich_citation_contexts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "draft_report_items", lambda config_arg, shortlist, use_llm=True: [])
    monkeypatch.setattr(pipeline, "review_draft", lambda config_arg, drafts, use_llm=True: [])
    monkeypatch.setattr(pipeline, "approve_publication", lambda config_arg, shortlist, drafts, reviews: [approved])
    monkeypatch.setattr(pipeline, "cache_selected_pdfs", lambda items, config_arg, run_date: items)

    result = pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 7, 9), dry_run=True, use_llm=False)

    assert order == ["openalex", "unpaywall", "paper_text"]
    assert result.items[0].material.pdf_url == "https://repository.test/pipeline-upw.pdf"


def test_quality_check_requires_unpaywall_full_profile(tmp_path, monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["unpaywall"] = {
        "enabled": False,
        "email_env": "UNPAYWALL_EMAIL",
        "max_papers_per_run": 5,
        "timeout_seconds": 5,
        "run_budget_seconds": 0,
        "cache_enabled": False,
        "cache_path": "",
        "cache_max_entries": 10,
    }
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)

    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert by_key["unpaywall"].status == "disabled"
    assert "enabled=false" in by_key["unpaywall"].detail
    assert "email_env=UNPAYWALL_EMAIL" in by_key["unpaywall"].detail
    assert "max_papers_per_run=5" in by_key["unpaywall"].detail
    assert "run_budget_seconds=0" in by_key["unpaywall"].detail
    assert "cache_enabled=false" in by_key["unpaywall"].detail
    assert "cache_path=<missing>" in by_key["unpaywall"].detail
    assert "cache_max_entries=10" in by_key["unpaywall"].detail


def test_unpaywall_links_render_in_markdown_and_html():
    from daily_agent.rendering.html import _paper_links as html_links
    from daily_agent.rendering.markdown import _paper_links as markdown_links

    material = MaterialRecord(
        key="doi:10.1234/render-upw",
        source="crossref",
        item_type="paper",
        title="Rendered Unpaywall paper",
        url="https://doi.org/10.1234/render-upw",
        doi="10.1234/render-upw",
        raw={"unpaywall_url": "https://repository.test/render-upw"},
        evidence={"sources": {"unpaywall": {"landing_url": "https://repository.test/render-upw"}}},
    )

    assert "[Unpaywall](https://repository.test/render-upw)" in markdown_links(material)
    assert ">Unpaywall</a>" in html_links(material)
