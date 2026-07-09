from datetime import date, datetime, timezone

import httpx

from daily_agent.config import load_config
from daily_agent.connectors.neurips import fetch_neurips
from daily_agent.connectors.openreview import fetch_openreview
from daily_agent.connectors.pmlr import fetch_pmlr
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
from daily_agent.pipeline import run_pipeline
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items
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


def test_openreview_fetch_normalizes_iclr_note(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["openreview"] = {
        "enabled": True,
        "max_results_per_venue": 2,
        "venues": [{"name": "ICLR 2026", "invitation": "ICLR.cc/2026/Conference/-/Submission"}],
    }

    def handler(url, **kwargs):
        assert url == "https://api2.openreview.net/notes"
        assert kwargs["params"]["invitation"] == "ICLR.cc/2026/Conference/-/Submission"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "notes": [
                    {
                        "id": "OR-1",
                        "cdate": 1770000000000,
                        "content": {
                            "title": {"value": "Quantum agents at ICLR"},
                            "abstract": {"value": "We train quantum agents."},
                            "authors": {"value": ["Alice", "Bob"]},
                            "pdf": {"value": "/pdf?id=OR-1"},
                            "venue": {"value": "ICLR 2026"},
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.openreview.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_openreview(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=30)

    assert items[0].source == "openreview"
    assert items[0].title == "Quantum agents at ICLR"
    assert items[0].abstract == "We train quantum agents."
    assert items[0].pdf_url == "https://openreview.net/pdf?id=OR-1"
    assert items[0].raw["openreview_id"] == "OR-1"
    assert items[0].raw["venue"] == "ICLR 2026"


def test_pmlr_fetch_normalizes_icml_volume(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["pmlr"] = {"enabled": True, "volumes": [{"venue": "ICML 2025", "url": "https://proceedings.mlr.press/v267/"}]}

    html = """
    <div class="paper">
      <p class="title">Quantum circuit learning at ICML</p>
      <p class="authors">Carol Chen, Dana Doe</p>
      <p class="abstract">We optimize circuits for learning.</p>
      <a href="paper1.html">abs</a>
      <a href="paper1.pdf">Download PDF</a>
    </div>
    """

    def handler(url, **kwargs):
        assert url == "https://proceedings.mlr.press/v267/"
        return httpx.Response(200, request=httpx.Request("GET", url), text=html)

    monkeypatch.setattr("daily_agent.connectors.pmlr.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_pmlr(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=365)

    assert items[0].source == "pmlr"
    assert items[0].title == "Quantum circuit learning at ICML"
    assert items[0].url == "https://proceedings.mlr.press/v267/paper1.html"
    assert items[0].pdf_url == "https://proceedings.mlr.press/v267/paper1.pdf"
    assert items[0].raw["venue"] == "ICML 2025"


def test_neurips_fetch_normalizes_proceedings_page(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["neurips"] = {"enabled": True, "years": [2025], "max_results_per_year": 2}

    html = """
    <li><a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">Quantum world models</a></li>
    """
    paper_html = """
    <h4>Abstract</h4><p>We build quantum world models.</p>
    <a href="/paper_files/paper/2025/file/abc-Paper-Conference.pdf">Paper</a>
    <i>Alice Author, Bob Builder</i>
    """

    def handler(url, **kwargs):
        if url == "https://papers.nips.cc/paper_files/paper/2025":
            return httpx.Response(200, request=httpx.Request("GET", url), text=html)
        assert url == "https://papers.nips.cc/paper_files/paper/2025/hash/abc-Abstract-Conference.html"
        return httpx.Response(200, request=httpx.Request("GET", url), text=paper_html)

    monkeypatch.setattr("daily_agent.connectors.neurips.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_neurips(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=365)

    assert items[0].source == "neurips"
    assert items[0].title == "Quantum world models"
    assert items[0].abstract == "We build quantum world models."
    assert items[0].pdf_url == "https://papers.nips.cc/paper_files/paper/2025/file/abc-Paper-Conference.pdf"
    assert items[0].raw["venue"] == "NeurIPS 2025"


def test_conference_dedup_and_markdown_links():
    openreview = DigestItem(
        id="OR-1",
        source="openreview",
        item_type="paper",
        title="Quantum agent paper",
        url="https://openreview.net/forum?id=OR-1",
        abstract="abstract",
        doi="10.1234/conf.1",
        raw={"openreview_id": "OR-1", "venue": "ICLR 2026", "openreview_url": "https://openreview.net/forum?id=OR-1"},
    )
    pmlr = DigestItem(
        id="pmlr-v267-paper1",
        source="pmlr",
        item_type="paper",
        title="Quantum agent paper",
        url="https://proceedings.mlr.press/v267/paper1.html",
        doi="10.1234/conf.1",
        raw={"pmlr_id": "pmlr-v267-paper1", "venue": "ICML 2025", "pmlr_url": "https://proceedings.mlr.press/v267/paper1.html"},
    )

    merged = deduplicate_items([openreview, pmlr])
    material = MaterialRecord.from_item(merged[0])
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )
    markdown = render_daily_markdown([approved], date(2026, 5, 27), RunStatus())

    assert len(merged) == 1
    assert material.source_aliases["openreview"] == "OR-1"
    assert material.source_aliases["pmlr"] == "pmlr-v267-paper1"
    assert "OpenReview / PMLR" in markdown
    assert "[OpenReview](https://openreview.net/forum?id=OR-1)" in markdown
    assert "[PMLR](https://proceedings.mlr.press/v267/paper1.html)" in markdown


def test_pipeline_fetches_conference_sources_and_records_health(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 1
    config.quota["paper_target"] = 1
    config.quota["github_target"] = 0
    for source in ["openreview", "pmlr", "neurips"]:
        config.sources[source] = {"enabled": True}

    paper = DigestItem(
        id="OR-1",
        source="openreview",
        item_type="paper",
        title="Quantum circuit optimization method",
        url="https://openreview.net/forum?id=OR-1",
        pdf_url="https://openreview.net/pdf?id=OR-1",
        abstract="We propose a machine learning method for quantum circuit optimization. Experiments show improved depth.",
        updated_at="2026-05-20T00:00:00+00:00",
        source_tags=["quantum_ai"],
        raw={"openreview_id": "OR-1", "venue": "ICLR 2026", "openreview_url": "https://openreview.net/forum?id=OR-1"},
    )

    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_openalex", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_google_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_crossref", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_dblp", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_ieee", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_openreview", lambda config, target_dt, window_days=None: [paper])
    monkeypatch.setattr("daily_agent.pipeline.fetch_pmlr", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_neurips", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.enrich_paper_texts", lambda records, config: records)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 27), dry_run=True, use_llm=False)
    health = load_health_report(config)

    assert result.items[0].source == "openreview"
    assert load_material_library(config)[result.items[0].key].source_aliases["openreview"] == "OR-1"
    assert any(source.name.startswith("OpenReview/") and source.item_count == 1 for source in result.status.sources)
    assert health["current"]["summary"]["source_coverage_count"] == 1
