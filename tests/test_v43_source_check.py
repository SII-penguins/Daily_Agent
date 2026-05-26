from datetime import date, datetime, timezone

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

    paper = DigestItem(id="W1", source="openalex", item_type="paper", title="Quantum source check", url="https://openalex.org/W1")

    monkeypatch.setattr("daily_agent.source_check.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openalex", lambda config, target_dt, window_days=None: [paper])
    monkeypatch.setattr("daily_agent.source_check.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_crossref", lambda config, target_dt, window_days=None: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("daily_agent.source_check.fetch_openreview", lambda config, target_dt, window_days=None: [])
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
