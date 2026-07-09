from datetime import date, datetime, timezone
import time

from daily_agent.cli import main
from daily_agent.config import load_config
from daily_agent.models import DigestItem
from daily_agent.source_check import SourceCheckResult, render_source_check, run_source_check


def test_source_check_reports_counts_failures_and_skips(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["openalex"] = {"enabled": True}
    config.sources["crossref"] = {"enabled": True}
    config.sources["ieee"] = {"enabled": True, "api_key_env": "IEEE_XPLORE_API_KEY"}
    config.sources["pmlr"] = {"enabled": False}
    monkeypatch.delenv("IEEE_XPLORE_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.source_check.scholarly_available", lambda: False)

    paper = DigestItem(id="W1", source="openalex", item_type="paper", title="Quantum source check", url="https://openalex.org/W1")

    monkeypatch.setattr("daily_agent.source_check.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openalex", lambda config, target_dt, window_days=None: [paper])
    monkeypatch.setattr("daily_agent.source_check.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_crossref", lambda config, target_dt, window_days=None: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("daily_agent.source_check.fetch_dblp", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_google_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_ieee", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openreview", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_pmlr", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_neurips", lambda config, target_dt, window_days=None: [])

    results = run_source_check(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=7, sample_limit=2)
    by_key = {result.key: result for result in results}

    assert by_key["openalex"].ok is True
    assert by_key["openalex"].item_count == 1
    assert by_key["openalex"].samples == ["Quantum source check"]
    assert by_key["crossref"].ok is False
    assert by_key["crossref"].error == "boom"
    assert by_key["ieee"].skipped is True
    assert by_key["ieee"].credential_status == "missing IEEE_XPLORE_API_KEY"
    assert by_key["pmlr"].skipped is True
    assert by_key["pmlr"].credential_status == "disabled"


def test_source_check_uses_probe_limits_instead_of_full_recall_limits(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["arxiv"] = {"enabled": True, "max_results_per_query": 50, "max_queries_per_domain": 6, "request_delay_seconds": 3}
    config.sources["github"] = {"enabled": True, "max_results_per_query": 50, "max_queries_per_domain": 20}
    config.sources["openalex"] = {"enabled": True, "max_results_per_query": 50, "max_queries_per_domain": 6, "timeout_seconds": 45}
    config.sources["openreview"] = {
        "enabled": True,
        "max_results_per_venue": 50,
        "venues": [{"name": "ICLR 2026"}, {"name": "ICLR 2025"}],
    }
    seen = {}

    def fake_arxiv(probe_config, target_dt, window_days=None):
        seen["arxiv"] = dict(probe_config.sources["arxiv"])
        return []

    def fake_openalex(probe_config, target_dt, window_days=None):
        seen["openalex"] = dict(probe_config.sources["openalex"])
        return []

    def fake_github(probe_config, target_dt, window_days=None):
        seen["github"] = dict(probe_config.sources["github"])
        return []

    def fake_openreview(probe_config, target_dt, window_days=None):
        seen["openreview"] = dict(probe_config.sources["openreview"])
        return []

    monkeypatch.setattr("daily_agent.source_check.fetch_arxiv", fake_arxiv)
    monkeypatch.setattr("daily_agent.source_check.fetch_github", fake_github)
    monkeypatch.setattr("daily_agent.source_check.fetch_openalex", fake_openalex)
    monkeypatch.setattr("daily_agent.source_check.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_crossref", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_dblp", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_google_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_ieee", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openreview", fake_openreview)
    monkeypatch.setattr("daily_agent.source_check.fetch_pmlr", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_neurips", lambda config, target_dt, window_days=None: [])

    run_source_check(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=7, sample_limit=2)

    assert seen["arxiv"]["max_results_per_query"] == 2
    assert seen["arxiv"]["max_queries_per_domain"] == 1
    assert seen["arxiv"]["request_delay_seconds"] == 0
    assert seen["openalex"]["max_results_per_query"] == 2
    assert seen["openalex"]["max_queries_per_domain"] == 1
    assert seen["openalex"]["timeout_seconds"] == 10
    assert seen["github"]["max_results_per_query"] == 2
    assert seen["github"]["max_queries_per_domain"] == 1
    assert seen["openreview"]["max_results_per_venue"] == 2
    assert len(seen["openreview"]["venues"]) == 1
    assert config.sources["arxiv"]["max_results_per_query"] == 50


def test_source_check_times_out_supervised_scholar_fallback_without_blocking_other_sources(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["google_scholar"] = {
        "enabled": True,
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": False,
        "source_check_timeout_seconds": 0.1,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setenv("DAILY_AGENT_SUPERVISED_SCHOLARLY_RUNTIME", "1")
    monkeypatch.setattr("daily_agent.source_check.scholarly_available", lambda: True)

    monkeypatch.setattr("daily_agent.source_check.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openalex", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_crossref", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_dblp", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_ieee", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openreview", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_pmlr", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_neurips", lambda config, target_dt, window_days=None: [])

    def slow_google_scholar(config, target_dt, window_days=None):
        time.sleep(5)
        return []

    monkeypatch.setattr("daily_agent.source_check.fetch_google_scholar", slow_google_scholar)

    start = time.monotonic()
    results = run_source_check(config, datetime(2026, 5, 27, tzinfo=timezone.utc), window_days=7, sample_limit=1)
    elapsed = time.monotonic() - start
    by_key = {result.key: result for result in results}

    assert elapsed < 2
    assert by_key["google_scholar"].ok is False
    assert "timed out" in by_key["google_scholar"].error
    assert by_key["openalex"].ok is True


def test_source_check_cli_prints_summary(tmp_path, monkeypatch, capsys):
    rows = [
        SourceCheckResult(key="openalex", name="OpenAlex", enabled=True, ok=True, item_count=2, samples=["A", "B"], credential_status="not required"),
        SourceCheckResult(key="ieee", name="IEEE", enabled=True, ok=True, skipped=True, item_count=0, credential_status="missing IEEE_XPLORE_API_KEY"),
    ]
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config("/Users/wuzixie/Daily_Agent"))
    monkeypatch.setattr("daily_agent.cli.run_source_check", lambda config, target_dt, window_days=7, sample_limit=3: rows)

    assert main(["source", "check", "--root", str(tmp_path), "--date", "2026-05-27", "--window-days", "7"]) == 0

    out = capsys.readouterr().out
    assert "Source check: 2026-05-27 window=7d" in out
    assert "OpenAlex ok 2 items" in out
    assert "samples: A; B" in out
    assert "IEEE skipped missing IEEE_XPLORE_API_KEY" in out


def test_render_source_check_marks_failure():
    text = render_source_check(
        [SourceCheckResult(key="crossref", name="Crossref", enabled=True, ok=False, error="timeout", credential_status="not required")],
        date(2026, 5, 27),
        window_days=7,
    )

    assert "Crossref failed timeout" in text
