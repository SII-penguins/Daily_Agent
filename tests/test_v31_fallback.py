from datetime import date, datetime, timezone
from urllib.parse import unquote_plus

import httpx

from daily_agent.cli import main
from daily_agent.config import DomainConfig, load_config
from daily_agent.connectors.arxiv import fetch_arxiv
from daily_agent.connectors.github import enrich_github_update_signals, fetch_github
from daily_agent.models import DigestItem, MaterialRecord, SelectedRecord
from daily_agent.pipeline import run_pipeline
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.storage import load_health_report, load_material_library, write_material_library, write_selected


def test_arxiv_fetch_uses_submitted_date_window_and_marks_historical(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["arxiv"]["request_delay_seconds"] = 0
    captured = {}
    feed = """
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>https://arxiv.org/abs/2401.00001v1</id>
        <updated>2026-04-20T00:00:00Z</updated>
        <published>2026-04-20T00:00:00Z</published>
        <title>Quantum fallback paper</title>
        <summary>A method for quantum circuit optimization.</summary>
        <author><name>Alice</name></author>
        <category term="quant-ph" />
      </entry>
    </feed>
    """

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            captured["url"] = url
            return httpx.Response(200, request=httpx.Request("GET", url), text=feed)

    monkeypatch.setattr("daily_agent.connectors.arxiv.httpx.Client", FakeClient)

    items = fetch_arxiv(config, datetime(2026, 5, 18, tzinfo=timezone.utc), window_days=30)

    query = unquote_plus(captured["url"])
    assert "submittedDate:[202604180000 TO 202605190000]" in query
    assert items[0].is_historical_supplement is True


def test_arxiv_fetch_retries_429_with_retry_after(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "domains", [
        DomainConfig(
            name="quantum_ai",
            quota_group="quantum",
            priority=1.0,
            description="",
            include_keywords=["quantum circuit"],
            exclude_keywords=[],
            arxiv_categories=["quant-ph"],
            github_queries=[],
        )
    ])
    config.sources["arxiv"]["request_delay_seconds"] = 0
    config.sources["arxiv"]["rate_limit_retries"] = 1
    sleeps = []
    feed = """
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>https://arxiv.org/abs/2401.00003v1</id>
        <updated>2026-05-18T00:00:00Z</updated>
        <published>2026-05-18T00:00:00Z</published>
        <title>Retried quantum paper</title>
        <summary>A method for quantum circuit optimization.</summary>
        <author><name>Alice</name></author>
        <category term="quant-ph" />
      </entry>
    </feed>
    """

    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            FakeClient.calls += 1
            request = httpx.Request("GET", url)
            if FakeClient.calls == 1:
                return httpx.Response(429, request=request, headers={"Retry-After": "4"})
            return httpx.Response(200, request=request, text=feed)

    monkeypatch.setattr("daily_agent.connectors.arxiv.httpx.Client", FakeClient)
    monkeypatch.setattr("daily_agent.connectors.arxiv.time.sleep", lambda seconds: sleeps.append(seconds))

    items = fetch_arxiv(config, datetime(2026, 5, 18, tzinfo=timezone.utc), window_days=7)

    assert FakeClient.calls == 2
    assert sleeps == [4.0]
    assert [item.title for item in items] == ["Retried quantum paper"]


def test_github_search_uses_pushed_window(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["github"]["trending_enabled"] = False
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            captured["params"] = kwargs.get("params", {})
            return httpx.Response(200, request=httpx.Request("GET", url), json={"items": []})

    monkeypatch.setattr("daily_agent.connectors.github.httpx.Client", FakeClient)

    fetch_github(config, datetime(2026, 5, 18, tzinfo=timezone.utc), window_days=90)

    assert "pushed:>=2026-02-17" in captured["params"]["q"]


def test_github_update_signal_enrichment_only_checks_history_matches(monkeypatch):
    history = {
        "github:owner/repo": SelectedRecord(
            key="github:owner/repo",
            selected_at="2026-05-17",
            source="github",
            item_type="repo",
            title="owner/repo",
            url="https://github.com/owner/repo",
        )
    }
    matched = DigestItem(id="owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    unmatched = DigestItem(id="owner/other", source="github", item_type="repo", title="owner/other", url="https://github.com/owner/other")
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, **kwargs):
            calls.append(url)
            request = httpx.Request("GET", url)
            if url.endswith("/repos/owner/repo/releases/latest"):
                return httpx.Response(200, request=request, json={"tag_name": "v1.1.0", "published_at": "2026-05-18T00:00:00Z"})
            if url.endswith("/repos/owner/repo/tags"):
                return httpx.Response(200, request=request, json=[{"name": "v1.1.0"}])
            raise AssertionError(url)

    monkeypatch.setattr("daily_agent.connectors.github.httpx.Client", FakeClient)

    enriched = enrich_github_update_signals([matched, unmatched], history)

    assert enriched == [matched, unmatched]
    assert calls == [
        "https://api.github.com/repos/owner/repo/releases/latest",
        "https://api.github.com/repos/owner/repo/tags",
    ]
    assert matched.raw["latest_release_tag"] == "v1.1.0"
    assert matched.raw["latest_release_published_at"] == "2026-05-18T00:00:00Z"
    assert matched.raw["latest_tag_name"] == "v1.1.0"
    assert unmatched.raw == {}


def test_pipeline_expands_fallback_window_when_shortlist_is_insufficient(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 2
    config.quota["paper_target"] = 2
    config.quota["github_target"] = 0
    config.sources["selection"]["top_candidates_for_llm"] = 2
    config.sources["arxiv"]["fallback_windows_days"] = [30]
    calls = []

    recent = DigestItem(
        id="2401.00001",
        source="arxiv",
        item_type="paper",
        title="Recent quantum circuit method",
        url="https://arxiv.org/abs/2401.00001v1",
        abstract="We propose a concrete method for quantum circuit optimization with machine learning and reported results.",
        categories=["quant-ph"],
        arxiv_id="2401.00001",
        arxiv_version="v1",
        updated_at="2026-05-18T00:00:00+00:00",
        pdf_url="https://arxiv.org/pdf/2401.00001v1",
    )
    older = DigestItem(
        id="2401.00002",
        source="arxiv",
        item_type="paper",
        title="Older quantum circuit method",
        url="https://arxiv.org/abs/2401.00002v1",
        abstract="We propose a concrete method for quantum circuit compilation with reported results.",
        categories=["quant-ph"],
        arxiv_id="2401.00002",
        arxiv_version="v1",
        updated_at="2026-04-25T00:00:00+00:00",
        pdf_url="https://arxiv.org/pdf/2401.00002v1",
        is_historical_supplement=True,
    )

    def fake_fetch_arxiv(config, target_dt, window_days=None):
        calls.append(window_days)
        return [recent] if window_days == 7 else [recent, older]

    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", fake_fetch_arxiv)
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert calls == [7, 30]
    assert {item.key for item in result.items} == {"arxiv:2401.00001", "arxiv:2401.00002"}
    assert result.status.fallback == "内容不足，已扩展检索窗口"
    assert (tmp_path / "data" / "selected" / "selected-2026-05-18.dry-run.json").exists()
    assert not (tmp_path / "data" / "selected" / "selected-2026-05-18.json").exists()
    assert load_material_library(config)["arxiv:2401.00002"].raw["is_historical_supplement"] is True
    assert load_health_report(config)["current"]["run_date"] == "2026-05-18"


def test_pipeline_enriches_github_update_signals_before_history_scoring(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 1
    config.sources["arxiv"]["fallback_windows_days"] = []
    config.sources["github"]["update_signal_max_repos"] = 1
    previous_material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for coding agents.",
        raw={
            "pushed_at": "2026-05-16T00:00:00Z",
            "latest_release_tag": "v1.0.0",
            "latest_release_published_at": "2026-05-16T00:00:00Z",
        },
    )
    write_selected(config, [previous_material], date(2026, 5, 17), dry_run=False)
    current = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for coding agents.",
        stars=105,
        language="Python",
        updated_at="2026-05-18T00:00:00+00:00",
        raw={"pushed_at": "2026-05-16T00:00:00Z"},
    )

    def fake_enrich(items, history, max_repos=10):
        assert max_repos == 1
        assert "github:owner/repo" in history
        items[0].raw["latest_release_tag"] = "v1.1.0"
        items[0].raw["latest_release_published_at"] = "2026-05-18T00:00:00Z"
        return items

    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [current])
    monkeypatch.setattr("daily_agent.pipeline.enrich_github_update_signals", fake_enrich)
    monkeypatch.setattr("daily_agent.editorial.enrich_github_readmes", lambda records: records)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=False, use_llm=False, delivery_mode="local")

    assert [item.key for item in result.items] == ["github:owner/repo"]
    assert result.items[0].material.update_label == "major_update"
    assert load_material_library(config)["github:owner/repo"].raw["latest_release_tag"] == "v1.1.0"


def test_pipeline_expansion_respects_recent_publication_suppression(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 1
    config.sources["selection"]["top_candidates_for_llm"] = 1
    config.sources["arxiv"]["fallback_windows_days"] = [30]
    published = MaterialRecord(
        key="arxiv:2401.00002",
        source="arxiv",
        item_type="paper",
        title="Older quantum circuit method",
        url="https://arxiv.org/abs/2401.00002v1",
        abstract="We propose a concrete method for quantum circuit compilation with reported results.",
        tags=["quantum_ai"],
        score=99,
        quality_status="published",
        published_dates=["2026-05-17"],
    )
    write_material_library(config, {published.key: published})
    older = DigestItem(
        id="2401.00002",
        source="arxiv",
        item_type="paper",
        title="Older quantum circuit method",
        url="https://arxiv.org/abs/2401.00002v1",
        abstract="We propose a concrete method for quantum circuit compilation with reported results.",
        categories=["quant-ph"],
        arxiv_id="2401.00002",
        arxiv_version="v1",
        updated_at="2026-04-25T00:00:00+00:00",
        pdf_url="https://arxiv.org/pdf/2401.00002v1",
        is_historical_supplement=True,
    )
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [older])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert result.items == []


def test_historical_supplement_label_renders_in_markdown():
    material = MaterialRecord(
        key="arxiv:2401.00002",
        source="arxiv",
        item_type="paper",
        title="Older quantum circuit method",
        url="https://arxiv.org/abs/2401.00002v1",
        abstract="We propose a concrete method for quantum circuit compilation with reported results.",
        tags=["quantum_ai"],
        raw={"is_historical_supplement": True},
    )
    approved = __import__("daily_agent.models").models.ApprovedItem(
        key=material.key,
        item_type=material.item_type,
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )
    markdown = render_daily_markdown([approved], date(2026, 5, 18), __import__("daily_agent.models").models.RunStatus())

    assert "【历史补充】" in markdown
