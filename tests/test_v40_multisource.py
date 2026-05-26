from datetime import date, datetime, timezone

import httpx

from daily_agent.config import load_config
from daily_agent.connectors.crossref import fetch_crossref
from daily_agent.connectors.ieee import fetch_ieee
from daily_agent.connectors.openalex import fetch_openalex
from daily_agent.connectors.semantic_scholar import fetch_semantic_scholar
from daily_agent.editorial import draft_report_items
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
from daily_agent.pipeline import run_pipeline
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.scoring.rules import score_items
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


def test_openalex_fetch_normalizes_work(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["openalex"] = {"enabled": True, "max_results_per_query": 2}

    def handler(url, **kwargs):
        assert url == "https://api.openalex.org/works"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W123",
                        "title": "Quantum circuit routing with learning",
                        "doi": "https://doi.org/10.1234/qc.1",
                        "publication_date": "2026-05-18",
                        "cited_by_count": 12,
                        "abstract_inverted_index": {"We": [0], "optimize": [1], "circuits": [2]},
                        "authorships": [{"author": {"display_name": "Alice"}}],
                        "concepts": [{"display_name": "Quantum computing"}],
                        "primary_location": {
                            "landing_page_url": "https://publisher.test/qc",
                            "pdf_url": "https://publisher.test/qc.pdf",
                            "source": {"display_name": "Journal of Quantum Tests"},
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.openalex.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_openalex(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7)

    assert len(items) == 1
    assert items[0].source == "openalex"
    assert items[0].item_type == "paper"
    assert items[0].doi == "10.1234/qc.1"
    assert items[0].pdf_url == "https://publisher.test/qc.pdf"
    assert items[0].abstract == "We optimize circuits"
    assert items[0].raw["openalex_id"] == "W123"
    assert items[0].raw["venue"] == "Journal of Quantum Tests"
    assert items[0].raw["cited_by_count"] == 12


def test_semantic_scholar_fetch_normalizes_paper(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["semantic_scholar"] = {"enabled": True, "max_results_per_query": 2}

    def handler(url, **kwargs):
        assert url.endswith("/graph/v1/paper/search")
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {
                        "paperId": "S2-1",
                        "title": "Quantum agents",
                        "abstract": "We study quantum agents.",
                        "url": "https://semanticscholar.org/paper/S2-1",
                        "publicationDate": "2026-05-18",
                        "venue": "NeurIPS",
                        "citationCount": 20,
                        "influentialCitationCount": 3,
                        "authors": [{"name": "Bob"}],
                        "externalIds": {"DOI": "10.1234/s2.1", "ArXiv": "2605.00001"},
                        "openAccessPdf": {"url": "https://paper.test/s2.pdf"},
                        "fieldsOfStudy": ["Computer Science"],
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.semantic_scholar.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_semantic_scholar(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7)

    assert items[0].source == "semantic_scholar"
    assert items[0].doi == "10.1234/s2.1"
    assert items[0].arxiv_id == "2605.00001"
    assert items[0].pdf_url == "https://paper.test/s2.pdf"
    assert items[0].raw["semantic_scholar_id"] == "S2-1"
    assert items[0].raw["citation_count"] == 20
    assert items[0].raw["influential_citation_count"] == 3


def test_crossref_fetch_normalizes_work(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["crossref"] = {"enabled": True, "max_results_per_query": 2}

    def handler(url, **kwargs):
        assert url == "https://api.crossref.org/works"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1234/cross.1",
                            "title": ["Crossref quantum paper"],
                            "abstract": "<jats:p>We compile circuits.</jats:p>",
                            "URL": "https://doi.org/10.1234/cross.1",
                            "published-online": {"date-parts": [[2026, 5, 18]]},
                            "container-title": ["IEEE Test Letters"],
                            "author": [{"given": "Carol", "family": "Chen"}],
                            "is-referenced-by-count": 7,
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("daily_agent.connectors.crossref.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_crossref(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7)

    assert items[0].source == "crossref"
    assert items[0].doi == "10.1234/cross.1"
    assert items[0].abstract == "We compile circuits."
    assert items[0].raw["venue"] == "IEEE Test Letters"
    assert items[0].raw["cited_by_count"] == 7


def test_ieee_fetch_skips_without_key(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["ieee"] = {"enabled": True, "api_key_env": "IEEE_XPLORE_API_KEY"}
    monkeypatch.delenv("IEEE_XPLORE_API_KEY", raising=False)

    assert fetch_ieee(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7) == []


def test_ieee_fetch_normalizes_metadata_with_key(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["ieee"] = {"enabled": True, "api_key_env": "IEEE_XPLORE_API_KEY", "max_results_per_query": 2}
    monkeypatch.setenv("IEEE_XPLORE_API_KEY", "key")

    def handler(url, **kwargs):
        assert url == "https://ieeexploreapi.ieee.org/api/v1/search/articles"
        assert kwargs["params"]["apikey"] == "key"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "articles": [
                    {
                        "article_number": "123",
                        "title": "IEEE quantum compilation",
                        "abstract": "We optimize quantum circuits.",
                        "html_url": "https://ieeexplore.ieee.org/document/123",
                        "publication_year": 2026,
                        "publication_title": "IEEE Transactions on Quantum Tests",
                        "doi": "10.1109/test.123",
                        "authors": {"authors": [{"full_name": "Dana Doe"}]},
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.ieee.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_ieee(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7)

    assert items[0].source == "ieee"
    assert items[0].doi == "10.1109/test.123"
    assert items[0].url == "https://ieeexplore.ieee.org/document/123"
    assert items[0].raw["ieee_article_number"] == "123"
    assert items[0].raw["venue"] == "IEEE Transactions on Quantum Tests"


def test_multisource_dedup_merges_doi_aliases_and_evidence():
    arxiv = DigestItem(
        id="2605.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum circuit method",
        url="https://arxiv.org/abs/2605.00001v1",
        pdf_url="https://arxiv.org/pdf/2605.00001v1",
        abstract="arxiv abstract",
        arxiv_id="2605.00001",
        doi="10.1234/qc.1",
        source_tags=["quantum_ai"],
        raw={"entry_id": "https://arxiv.org/abs/2605.00001v1"},
    )
    openalex = DigestItem(
        id="W123",
        source="openalex",
        item_type="paper",
        title="Quantum circuit method",
        url="https://openalex.org/W123",
        abstract="openalex abstract",
        doi="10.1234/qc.1",
        raw={"openalex_id": "W123", "cited_by_count": 9},
    )

    merged = deduplicate_items([arxiv, openalex])

    assert len(merged) == 1
    assert merged[0].canonical_key() == "arxiv:2605.00001"
    assert merged[0].pdf_url == "https://arxiv.org/pdf/2605.00001v1"
    assert merged[0].raw["source_aliases"]["arxiv"] == "2605.00001"
    assert merged[0].raw["source_aliases"]["openalex"] == "W123"
    assert "arxiv" in merged[0].raw["evidence"]["sources"]
    assert "openalex" in merged[0].raw["evidence"]["sources"]


def test_multisource_scoring_adds_impact_and_evidence_signal():
    config = load_config("/Users/wuzixie/Daily_Agent")
    item = DigestItem(
        id="W123",
        source="openalex",
        item_type="paper",
        title="Quantum circuit optimization",
        url="https://openalex.org/W123",
        pdf_url="https://publisher.test/qc.pdf",
        abstract="A method for quantum circuit optimization.",
        doi="10.1234/qc.1",
        updated_at="2026-05-18T00:00:00+00:00",
        raw={"cited_by_count": 100, "open_access_url": "https://publisher.test/qc.pdf"},
    )

    scored = score_items([item], config, {}, datetime(2026, 5, 19, tzinfo=timezone.utc))[0]

    assert scored.score_breakdown["scholarly_impact"] > 0
    assert scored.score_breakdown["evidence"] > 0


def test_markdown_renders_multisource_paper_links():
    material = MaterialRecord(
        key="doi:10.1234/qc.1",
        source="openalex",
        item_type="paper",
        title="Quantum paper",
        url="https://openalex.org/W123",
        pdf_url="https://publisher.test/qc.pdf",
        doi="10.1234/qc.1",
        source_aliases={"openalex": "W123", "semantic_scholar": "S2-1", "ieee": "123"},
        raw={
            "published_at": "2026-05-18",
            "semantic_scholar_url": "https://semanticscholar.org/paper/S2-1",
            "ieee_url": "https://ieeexplore.ieee.org/document/123",
        },
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )

    markdown = render_daily_markdown([approved], date(2026, 5, 19), RunStatus())

    assert "OpenAlex / Semantic Scholar / IEEE" in markdown
    assert "[DOI](https://doi.org/10.1234/qc.1)" in markdown
    assert "[OpenAlex](https://openalex.org/W123)" in markdown
    assert "[Semantic Scholar](https://semanticscholar.org/paper/S2-1)" in markdown
    assert "[IEEE](https://ieeexplore.ieee.org/document/123)" in markdown


def test_rule_writer_uses_multisource_evidence_when_primary_abstract_is_missing():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="doi:10.1234/qc.1",
        source="openalex",
        item_type="paper",
        title="Quantum circuit method",
        url="https://openalex.org/W123",
        doi="10.1234/qc.1",
        source_aliases={"openalex": "W123", "semantic_scholar": "S2-1"},
        evidence={
            "sources": {
                "openalex": {
                    "abstract": "We propose a method for quantum circuit optimization. Experiments show reduced depth.",
                    "venue": "Journal of Quantum Tests",
                    "citation_count": 11,
                },
                "semantic_scholar": {"citation_count": 13},
            }
        },
    )

    draft = draft_report_items(config, [material], use_llm=False)[0]

    assert "We propose a method for quantum circuit optimization" in draft.evidence_used[0]
    assert "sources=openalex,semantic_scholar" in draft.draft_fields["evidence_from_source"]
    assert "venue=Journal of Quantum Tests" in draft.draft_fields["evidence_from_source"]


def test_pipeline_fetches_multisource_and_records_health(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.delenv("IEEE_XPLORE_API_KEY", raising=False)
    config.quota["max_items"] = 1
    config.quota["paper_target"] = 1
    config.quota["github_target"] = 0
    config.sources["openalex"] = {"enabled": True}
    config.sources["semantic_scholar"] = {"enabled": True}
    config.sources["crossref"] = {"enabled": True}
    config.sources["ieee"] = {"enabled": True}

    paper = DigestItem(
        id="W123",
        source="openalex",
        item_type="paper",
        title="Quantum circuit optimization method",
        url="https://openalex.org/W123",
        pdf_url="https://publisher.test/qc.pdf",
        abstract="We propose a machine learning method for quantum circuit optimization. Experiments show improved depth.",
        doi="10.1234/qc.1",
        updated_at="2026-05-18T00:00:00+00:00",
        source_tags=["quantum_ai"],
        raw={"openalex_id": "W123", "cited_by_count": 12, "open_access_url": "https://publisher.test/qc.pdf"},
    )

    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_openalex", lambda config, target_dt, window_days=None: [paper])
    monkeypatch.setattr("daily_agent.pipeline.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_crossref", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_ieee", lambda config, target_dt, window_days=None: [])

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 19), dry_run=True, use_llm=False)
    health = load_health_report(config)

    assert [item.key for item in result.items] == ["doi:10.1234/qc.1"]
    assert load_material_library(config)["doi:10.1234/qc.1"].source_aliases["openalex"] == "W123"
    ieee_status = next(source for source in result.status.sources if source.name.startswith("IEEE/"))
    assert ieee_status.skipped
    assert ieee_status.error == "missing IEEE_XPLORE_API_KEY"
    assert "IEEE/7d 跳过（missing IEEE_XPLORE_API_KEY）" in result.daily_markdown
    assert health["current"]["summary"]["source_coverage_count"] == 1
    assert health["current"]["summary"]["approved_source_diversity"] == 1
    assert any(source["name"].startswith("IEEE/") and source["skipped"] for source in health["current"]["signals"]["sources"])
