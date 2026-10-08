from pathlib import Path
from datetime import date, datetime, timezone
import time

import httpx

from daily_agent.config import load_config
from daily_agent.connectors.google_scholar import _enrich_serpapi_citations, fetch_google_scholar, google_scholar_skip_reason
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
from daily_agent.pipeline import run_pipeline
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.source_check import run_source_check
from daily_agent.storage import load_health_report, load_material_library


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def test_google_scholar_serpapi_fetch_normalizes_result(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "max_results_per_query": 2,
    }
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")

    def handler(url, **kwargs):
        assert url == "https://serpapi.com/search.json"
        assert kwargs["params"]["engine"] == "google_scholar"
        assert kwargs["params"]["api_key"] == "serp-key"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "organic_results": [
                    {
                        "result_id": "GS-1",
                        "title": "Quantum circuit routing with hardware constraints",
                        "link": "https://publisher.test/qc",
                        "snippet": "We propose a hardware-aware routing method. doi:10.1234/gs.1",
                        "publication_info": {
                            "summary": "Alice Author, Bob Builder - Quantum Tests, 2026",
                            "authors": [{"name": "Alice Author"}, {"name": "Bob Builder"}],
                        },
                        "inline_links": {
                            "serpapi_cite_link": "https://serpapi.com/search.json?engine=google_scholar_cite&q=GS-1",
                            "cited_by": {"total": 42, "link": "https://scholar.google.com/scholar?cites=1", "cites_id": "1"},
                            "related_pages_link": "https://scholar.google.com/scholar?q=related:GS-1:scholar.google.com/",
                            "serpapi_related_pages_link": "https://serpapi.com/search.json?engine=google_scholar&q=related:GS-1",
                            "versions": {
                                "total": 3,
                                "link": "https://scholar.google.com/scholar?cluster=1",
                                "cluster_id": "1",
                                "serpapi_scholar_link": "https://serpapi.com/search.json?engine=google_scholar&cluster=1",
                            },
                            "cached_page_link": "https://scholar.googleusercontent.com/scholar?q=cache:GS-1",
                        },
                        "resources": [
                            {"title": "PDF", "file_format": "PDF", "link": "https://publisher.test/qc.pdf"},
                            {"title": "Full text PDF", "file_format": "PDF", "link": "https://open.test/qc-full.pdf"},
                        ],
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.google_scholar.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert len(items) == 1
    assert items[0].source == "google_scholar"
    assert items[0].doi == "10.1234/gs.1"
    assert items[0].pdf_url == "https://publisher.test/qc.pdf"
    assert items[0].authors == ["Alice Author", "Bob Builder"]
    assert items[0].published_at == "2026"
    assert items[0].raw["google_scholar_id"] == "GS-1"
    assert items[0].raw["citation_count"] == 42
    assert items[0].raw["google_scholar_cites_id"] == "1"
    assert items[0].raw["google_scholar_related_url"] == "https://scholar.google.com/scholar?q=related:GS-1:scholar.google.com/"
    assert items[0].raw["google_scholar_versions_url"] == "https://scholar.google.com/scholar?cluster=1"
    assert items[0].raw["google_scholar_versions_count"] == 3
    assert items[0].raw["google_scholar_cached_url"] == "https://scholar.googleusercontent.com/scholar?q=cache:GS-1"
    assert items[0].raw["serpapi_cite_url"] == "https://serpapi.com/search.json?engine=google_scholar_cite&q=GS-1"
    assert items[0].raw["serpapi_related_pages_url"] == "https://serpapi.com/search.json?engine=google_scholar&q=related:GS-1"
    assert items[0].raw["serpapi_versions_url"] == "https://serpapi.com/search.json?engine=google_scholar&cluster=1"
    assert items[0].raw["pdf_urls"] == ["https://publisher.test/qc.pdf", "https://open.test/qc-full.pdf"]
    material = MaterialRecord.from_item(items[0])
    assert material.evidence["sources"]["google_scholar"]["pdf_urls"] == ["https://publisher.test/qc.pdf", "https://open.test/qc-full.pdf"]


def test_google_scholar_serpapi_enriches_cite_endpoint_links_and_formats(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "max_results_per_query": 1,
        "max_queries_per_domain": 1,
        "cite_enrichment_enabled": True,
        "cite_enrichment_max_results_per_run": 10,
        "cite_cache_enabled": False,
    }
    config.domains[0].include_keywords[:] = ["quantum compilation"]
    object.__setattr__(config, "domains", [config.domains[0]])
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    seen_engines: list[str] = []

    def handler(url, **kwargs):
        params = kwargs["params"]
        seen_engines.append(params["engine"])
        if params["engine"] == "google_scholar":
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "organic_results": [
                        {
                            "result_id": "GS-CITE-1",
                            "title": "Scholar citation enriched paper",
                            "link": "https://publisher.test/cite",
                            "snippet": "A paper with official citation metadata.",
                            "publication_info": {"summary": "Alice Author - Scholar Venue, 2026"},
                            "inline_links": {
                                "serpapi_cite_link": "https://serpapi.com/search.json?engine=google_scholar_cite&q=GS-CITE-1"
                            },
                        }
                    ]
                },
            )
        assert params["engine"] == "google_scholar_cite"
        assert params["q"] == "GS-CITE-1"
        assert params["api_key"] == "serp-key"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "citations": [
                    {"title": "MLA", "snippet": "Author. Scholar citation enriched paper. Scholar Venue, 2026."},
                    {"title": "APA", "snippet": "Author. (2026). Scholar citation enriched paper."},
                ],
                "links": [
                    {"name": "BibTeX", "link": "https://scholar.test/scholar.bib?q=GS-CITE-1"},
                    {"name": "EndNote", "link": "https://scholar.test/scholar.enw?q=GS-CITE-1"},
                    {"name": "RefMan", "link": "https://scholar.test/scholar.ris?q=GS-CITE-1"},
                    {"name": "RefWorks", "link": "https://scholar.test/refworks?q=GS-CITE-1"},
                ],
            },
        )

    monkeypatch.setattr("daily_agent.connectors.google_scholar.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert seen_engines == ["google_scholar", "google_scholar_cite"]
    assert items[0].raw["google_scholar_citation_formats"]["MLA"].startswith("Author.")
    assert items[0].raw["google_scholar_citation_formats"]["APA"].startswith("Author.")
    assert items[0].raw["google_scholar_bibtex_url"] == "https://scholar.test/scholar.bib?q=GS-CITE-1"
    assert items[0].raw["google_scholar_endnote_url"] == "https://scholar.test/scholar.enw?q=GS-CITE-1"
    assert items[0].raw["google_scholar_refman_url"] == "https://scholar.test/scholar.ris?q=GS-CITE-1"
    assert items[0].raw["google_scholar_refworks_url"] == "https://scholar.test/refworks?q=GS-CITE-1"


def test_google_scholar_cite_enrichment_stops_at_run_budget(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    source_config = {
        "cite_enrichment_enabled": True,
        "cite_enrichment_max_results_per_run": 10,
        "cite_enrichment_run_budget_seconds": 5,
        "cite_cache_enabled": False,
        "timeout_seconds": 60,
    }
    items = [
        DigestItem(
            id="GS-BUDGET-1",
            source="google_scholar",
            item_type="paper",
            title="Budgeted Scholar paper one",
            url="https://publisher.test/budget-1",
            raw={"google_scholar_id": "GS-BUDGET-1"},
        ),
        DigestItem(
            id="GS-BUDGET-2",
            source="google_scholar",
            item_type="paper",
            title="Budgeted Scholar paper two",
            url="https://publisher.test/budget-2",
            raw={"google_scholar_id": "GS-BUDGET-2"},
        ),
    ]
    now = [100.0]
    calls: list[dict] = []

    class BudgetClient:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            now[0] = 106.0
            return httpx.Response(500, request=httpx.Request("GET", url))

    monkeypatch.setattr("daily_agent.connectors.google_scholar.time.monotonic", lambda: now[0])

    _enrich_serpapi_citations(BudgetClient(), items, source_config, "serp-key", config)

    assert len(calls) == 1
    assert calls[0]["timeout"] == 5.0


def test_google_scholar_cite_enrichment_reuses_persistent_cache(tmp_path, monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "max_results_per_query": 1,
        "max_queries_per_domain": 1,
        "cite_enrichment_enabled": True,
        "cite_enrichment_max_results_per_run": 10,
        "cite_cache_enabled": True,
        "cite_cache_path": "data/cache/google_scholar_cites.json",
        "cite_cache_max_entries": 50_000,
    }
    config.domains[0].include_keywords[:] = ["quantum compilation"]
    object.__setattr__(config, "domains", [config.domains[0]])
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    calls: list[str] = []

    def first_handler(url, **kwargs):
        params = kwargs["params"]
        calls.append(params["engine"])
        if params["engine"] == "google_scholar":
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "organic_results": [
                        {
                            "result_id": "GS-CACHED-CITE",
                            "title": "Cached Scholar citation paper",
                            "link": "https://publisher.test/cached-cite",
                            "snippet": "A paper whose Scholar cite metadata should be cached.",
                            "publication_info": {"summary": "Alice Author - Scholar Venue, 2026"},
                        }
                    ]
                },
            )
        assert params["engine"] == "google_scholar_cite"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "citations": [{"title": "APA", "snippet": "Author. (2026). Cached Scholar citation paper."}],
                "links": [
                    {"name": "BibTeX", "link": "https://scholar.test/cached.bib"},
                    {"name": "EndNote", "link": "https://scholar.test/cached.enw"},
                ],
            },
        )

    monkeypatch.setattr("daily_agent.connectors.google_scholar.httpx.Client", lambda **kwargs: _Client(first_handler, **kwargs))

    first_items = fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert calls == ["google_scholar", "google_scholar_cite"]
    assert first_items[0].raw["google_scholar_bibtex_url"] == "https://scholar.test/cached.bib"
    assert (tmp_path / "data" / "cache" / "google_scholar_cites.json").exists()

    def second_handler(url, **kwargs):
        params = kwargs["params"]
        calls.append(params["engine"])
        if params["engine"] == "google_scholar_cite":
            raise AssertionError("cite endpoint should not be called when cache has this Scholar id")
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "organic_results": [
                    {
                        "result_id": "GS-CACHED-CITE",
                        "title": "Cached Scholar citation paper",
                        "link": "https://publisher.test/cached-cite",
                        "snippet": "A paper whose Scholar cite metadata should be cached.",
                        "publication_info": {"summary": "Alice Author - Scholar Venue, 2026"},
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.google_scholar.httpx.Client", lambda **kwargs: _Client(second_handler, **kwargs))

    second_items = fetch_google_scholar(config, datetime(2026, 5, 28, tzinfo=timezone.utc), window_days=30)

    assert calls == ["google_scholar", "google_scholar_cite", "google_scholar"]
    assert second_items[0].raw["google_scholar_cite_enrichment"]["status"] == "cache_hit"
    assert second_items[0].raw["google_scholar_citation_formats"]["APA"].startswith("Author.")
    assert second_items[0].raw["google_scholar_bibtex_url"] == "https://scholar.test/cached.bib"
    assert second_items[0].raw["google_scholar_endnote_url"] == "https://scholar.test/cached.enw"


def test_google_scholar_serpapi_sorts_by_date_and_paginates(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "max_results_per_query": 45,
        "max_queries_per_domain": 1,
        "sort_by_date": True,
        "scisbd": 2,
    }
    config.domains[0].include_keywords[:] = ["quantum compilation"]
    object.__setattr__(config, "domains", [config.domains[0]])
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    seen_params: list[dict] = []

    def handler(url, **kwargs):
        params = dict(kwargs["params"])
        seen_params.append(params)
        start = int(params.get("start", 0))
        page_index = start // 20
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "organic_results": [
                    {
                        "result_id": f"GS-page-{page_index}",
                        "title": f"Fresh Scholar Paper {page_index}",
                        "link": f"https://publisher.test/{page_index}",
                        "snippet": "recent quantum compilation result",
                        "publication_info": {"summary": f"Author - Venue, 2026"},
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.google_scholar.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert [params["num"] for params in seen_params] == [20, 20, 5]
    assert [params.get("start", 0) for params in seen_params] == [0, 20, 40]
    assert all(params["scisbd"] == 2 for params in seen_params)
    assert [item.id for item in items] == ["GS-page-0", "GS-page-1", "GS-page-2"]


def test_google_scholar_dedup_merges_with_cross_source_doi_and_renders_link():
    arxiv = DigestItem(
        id="2605.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum circuit routing with hardware constraints",
        url="https://arxiv.org/abs/2605.00001v1",
        pdf_url="https://arxiv.org/pdf/2605.00001v1",
        abstract="arxiv abstract",
        arxiv_id="2605.00001",
        doi="10.1234/gs.1",
    )
    scholar = DigestItem(
        id="GS-1",
        source="google_scholar",
        item_type="paper",
        title="Quantum circuit routing with hardware constraints",
        url="https://publisher.test/qc",
        doi="10.1234/gs.1",
        raw={
            "google_scholar_id": "GS-1",
            "google_scholar_url": "https://scholar.google.com/scholar?q=Quantum+circuit+routing",
            "citation_count": 42,
            "google_scholar_cited_by_url": "https://scholar.google.com/scholar?cites=1",
            "google_scholar_related_url": "https://scholar.google.com/scholar?q=related:GS-1:scholar.google.com/",
            "google_scholar_versions_url": "https://scholar.google.com/scholar?cluster=1",
            "google_scholar_bibtex_url": "https://scholar.test/scholar.bib?q=GS-1",
            "google_scholar_endnote_url": "https://scholar.test/scholar.enw?q=GS-1",
        },
    )

    merged = deduplicate_items([arxiv, scholar])
    material = MaterialRecord.from_item(merged[0])
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "why_it_works": "w", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )
    markdown = render_daily_markdown([approved], date(2026, 5, 27), RunStatus())

    assert len(merged) == 1
    assert merged[0].canonical_key() == "arxiv:2605.00001"
    assert merged[0].raw["source_aliases"]["google_scholar"] == "GS-1"
    assert "google_scholar" in merged[0].raw["evidence"]["sources"]
    assert "Google Scholar" in markdown
    assert "[Google Scholar](https://scholar.google.com/scholar?q=Quantum+circuit+routing)" in markdown
    assert "[Scholar cited by](https://scholar.google.com/scholar?cites=1)" in markdown
    assert "[Scholar related](https://scholar.google.com/scholar?q=related:GS-1:scholar.google.com/)" in markdown
    assert "[Scholar versions](https://scholar.google.com/scholar?cluster=1)" in markdown
    assert "[Scholar BibTeX](https://scholar.test/scholar.bib?q=GS-1)" in markdown
    assert "[Scholar EndNote](https://scholar.test/scholar.enw?q=GS-1)" in markdown


def test_source_check_skips_google_scholar_without_serpapi_or_scholarly(tmp_path, monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.source_check.scholarly_available", lambda: False)

    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_crossref",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(f"daily_agent.source_check.{name}", lambda config, target_dt, window_days=None: [])

    results = run_source_check(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=7)
    by_key = {result.key: result for result in results}

    assert by_key["google_scholar"].skipped is True
    assert by_key["google_scholar"].credential_status == "missing SERPAPI_API_KEY and scholarly package"


def test_google_scholar_does_not_run_scholarly_fallback_unless_runtime_enabled(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": False,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.connectors.google_scholar.scholarly_available", lambda: True)
    monkeypatch.setattr(
        "daily_agent.connectors.google_scholar._fetch_scholarly",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("scholarly fallback should not run")),
    )

    assert "scholarly runtime fallback disabled" in google_scholar_skip_reason(config)
    assert fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30) == []


def test_google_scholar_can_run_scholarly_when_runtime_enabled(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": True,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.connectors.google_scholar.scholarly_available", lambda: True)
    monkeypatch.setattr("daily_agent.connectors.google_scholar._fetch_scholarly", lambda *args, **kwargs: ["used"])

    assert google_scholar_skip_reason(config) is None
    assert fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30) == ["used"]


def test_google_scholar_scholarly_runtime_budget_returns_empty_when_fallback_hangs(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": True,
        "scholarly_runtime_timeout_seconds": 0.1,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.connectors.google_scholar.scholarly_available", lambda: True)

    def slow_scholarly(*args, **kwargs):
        time.sleep(5)
        return ["too-late"]

    monkeypatch.setattr("daily_agent.connectors.google_scholar._fetch_scholarly", slow_scholarly)

    started = time.monotonic()
    items = fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert items == []
    assert time.monotonic() - started < 2


def test_google_scholar_can_run_scholarly_when_supervised_env_override_is_enabled(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["google_scholar"] = {
        "enabled": True,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": False,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setenv("DAILY_AGENT_SUPERVISED_SCHOLARLY_RUNTIME", "1")
    monkeypatch.setattr("daily_agent.connectors.google_scholar.scholarly_available", lambda: True)
    monkeypatch.setattr("daily_agent.connectors.google_scholar._fetch_scholarly", lambda *args, **kwargs: ["used-by-env"])

    assert google_scholar_skip_reason(config) is None
    assert fetch_google_scholar(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30) == ["used-by-env"]


def test_pipeline_fetches_google_scholar_and_records_health(tmp_path, monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 1
    config.quota["paper_target"] = 1
    config.quota["github_target"] = 0
    config.sources["google_scholar"] = {"enabled": True}
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key")
    paper = DigestItem(
        id="GS-1",
        source="google_scholar",
        item_type="paper",
        title="Quantum circuit routing with hardware constraints",
        url="https://publisher.test/qc",
        pdf_url="https://publisher.test/qc.pdf",
        abstract="We propose a hardware-aware quantum circuit routing method. Experiments show reduced depth.",
        doi="10.1234/gs.1",
        updated_at="2026-05-26T00:00:00+00:00",
        source_tags=["quantum_ai"],
        raw={"google_scholar_id": "GS-1", "google_scholar_url": "https://scholar.google.com/scholar?q=qc", "citation_count": 42},
    )

    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_crossref",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(f"daily_agent.pipeline.{name}", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_google_scholar", lambda config, target_dt, window_days=None: [paper])
    monkeypatch.setattr("daily_agent.pipeline.enrich_paper_texts", lambda records, config: records)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 27), dry_run=True, use_llm=False)
    health = load_health_report(config)

    assert [item.key for item in result.items] == ["doi:10.1234/gs.1"]
    assert load_material_library(config)["doi:10.1234/gs.1"].source_aliases["google_scholar"] == "GS-1"
    assert any(source.name.startswith("Google Scholar/") and source.item_count == 1 for source in result.status.sources)
    assert health["current"]["summary"]["source_coverage_count"] == 1
