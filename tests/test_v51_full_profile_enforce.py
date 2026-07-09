from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _copy_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ["sources.yaml", "interests.yaml", "delivery.yaml", "feedback.yaml"]:
        shutil.copy(ROOT / "config" / name, config_dir / name)
    return tmp_path


def test_enforce_full_profile_dry_run_reports_changes_without_writing(tmp_path):
    from daily_agent.full_profile import enforce_full_profile

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    feedback_path = root / "config" / "feedback.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    feedback = yaml.safe_load(feedback_path.read_text(encoding="utf-8"))
    sources["paper_text"]["max_papers_per_run"] = 3
    sources["paper_text"]["section_notes_enabled"] = False
    sources["google_scholar"]["enabled"] = False
    sources["google_scholar"]["sort_by_date"] = False
    sources["google_scholar"]["scisbd"] = 0
    sources["google_scholar"]["cite_enrichment_enabled"] = False
    sources["google_scholar"]["cite_enrichment_max_results_per_run"] = 3
    sources["google_scholar"]["cite_cache_enabled"] = False
    sources["google_scholar"]["cite_cache_path"] = "tmp/google_scholar_cites.json"
    sources["google_scholar"]["cite_cache_max_entries"] = 10
    sources["openalex"]["allowed_publication_types"] = ["article"]
    sources["semantic_scholar"].pop("allowed_publication_types", None)
    sources["crossref"]["allowed_publication_types"] = []
    sources["github"]["max_queries_per_domain"] = 2
    sources["github"].pop("trending_languages", None)
    sources["openreview"].pop("venues", None)
    sources["pmlr"].pop("volumes", None)
    sources["neurips"].pop("years", None)
    sources["citation_context"] = {"enabled": False, "max_papers_per_run": 1}
    sources["citation_discovery"] = {"enabled": False, "max_seed_papers": 2, "max_results_per_seed": 1}
    sources["oa_resolver"] = {"enabled": False, "max_papers_per_run": 5, "run_budget_seconds": 0}
    sources["unpaywall"] = {
        "enabled": False,
        "email_env": "OLD_EMAIL",
        "max_papers_per_run": 5,
        "run_budget_seconds": 0,
        "cache_enabled": False,
        "cache_path": "tmp/unpaywall.json",
        "cache_max_entries": 10,
    }
    sources["pdf_cache"] = {"enabled": False, "max_papers_per_run": 2, "max_urls_per_paper": 1}
    sources["insights"]["max_insights"] = 1
    sources["selection"]["repeat_suppression_days"] = 7
    sources["selection"]["historical_min_score"] = 99
    sources["selection"]["topic_relevance_gate_enabled"] = False
    sources["selection"]["min_topic_relevance_score"] = 1
    sources["exports"] = {"bibtex_enabled": False, "ris_enabled": False, "csv_enabled": False, "endnote_xml_enabled": False}
    feedback["events"]["enabled"] = False
    feedback["events"]["lookback_days"] = 14
    feedback["feishu_comments"] = {"enabled": False, "page_size": 10, "include_replies": False}
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")
    feedback_path.write_text(yaml.safe_dump(feedback, allow_unicode=True, sort_keys=False), encoding="utf-8")
    before = sources_path.read_text(encoding="utf-8")
    feedback_before = feedback_path.read_text(encoding="utf-8")

    result = enforce_full_profile(root, write=False)

    assert result.changed is True
    assert "config/sources.yaml: paper_text.max_papers_per_run 3 -> 50" in result.lines
    assert "config/sources.yaml: paper_text.section_notes_enabled False -> True" in result.lines
    assert "config/sources.yaml: google_scholar.enabled False -> True" in result.lines
    assert "config/sources.yaml: google_scholar.sort_by_date False -> True" in result.lines
    assert "config/sources.yaml: google_scholar.scisbd 0 -> 2" in result.lines
    assert "config/sources.yaml: google_scholar.cite_enrichment_enabled False -> True" in result.lines
    assert "config/sources.yaml: google_scholar.cite_enrichment_max_results_per_run 3 -> 50" in result.lines
    assert "config/sources.yaml: google_scholar.cite_cache_enabled False -> True" in result.lines
    assert "config/sources.yaml: google_scholar.cite_cache_path tmp/google_scholar_cites.json -> data/cache/google_scholar_cites.json" in result.lines
    assert "config/sources.yaml: google_scholar.cite_cache_max_entries 10 -> 50000" in result.lines
    assert "config/sources.yaml: openalex.allowed_publication_types ['article'] -> ['article', 'review', 'preprint', 'proceedings-article']" in result.lines
    assert "config/sources.yaml: semantic_scholar.allowed_publication_types <missing> -> ['JournalArticle', 'Conference', 'Review', 'Preprint']" in result.lines
    assert "config/sources.yaml: crossref.allowed_publication_types [] -> ['journal-article', 'proceedings-article', 'posted-content']" in result.lines
    assert "config/sources.yaml: github.max_queries_per_domain 2 -> 20" in result.lines
    assert "config/sources.yaml: github.trending_languages <missing> -> ['python', 'typescript', 'jupyter-notebook', 'rust']" in result.lines
    assert "config/sources.yaml: openreview.venues <missing> ->" in "\n".join(result.lines)
    assert "config/sources.yaml: pmlr.volumes <missing> ->" in "\n".join(result.lines)
    assert "config/sources.yaml: neurips.years <missing> -> [2026, 2025, 2024]" in result.lines
    assert "config/sources.yaml: citation_context.enabled False -> True" in result.lines
    assert "config/sources.yaml: citation_context.max_papers_per_run 1 -> 30" in result.lines
    assert "config/sources.yaml: citation_discovery.enabled False -> True" in result.lines
    assert "config/sources.yaml: citation_discovery.max_seed_papers 2 -> 20" in result.lines
    assert "config/sources.yaml: citation_discovery.max_results_per_seed 1 -> 5" in result.lines
    assert "config/sources.yaml: oa_resolver.enabled False -> True" in result.lines
    assert "config/sources.yaml: oa_resolver.max_papers_per_run 5 -> 50" in result.lines
    assert "config/sources.yaml: oa_resolver.run_budget_seconds 0 -> 120" in result.lines
    assert "config/sources.yaml: unpaywall.enabled False -> True" in result.lines
    assert "config/sources.yaml: unpaywall.email_env OLD_EMAIL -> UNPAYWALL_EMAIL" in result.lines
    assert "config/sources.yaml: unpaywall.max_papers_per_run 5 -> 50" in result.lines
    assert "config/sources.yaml: unpaywall.run_budget_seconds 0 -> 120" in result.lines
    assert "config/sources.yaml: unpaywall.cache_enabled False -> True" in result.lines
    assert "config/sources.yaml: unpaywall.cache_path tmp/unpaywall.json -> data/cache/unpaywall.json" in result.lines
    assert "config/sources.yaml: unpaywall.cache_max_entries 10 -> 50000" in result.lines
    assert "config/sources.yaml: pdf_cache.enabled False -> True" in result.lines
    assert "config/sources.yaml: pdf_cache.max_papers_per_run 2 -> 10" in result.lines
    assert "config/sources.yaml: pdf_cache.max_urls_per_paper 1 -> 4" in result.lines
    assert "config/sources.yaml: insights.max_insights 1 -> 5" in result.lines
    assert "config/sources.yaml: selection.repeat_suppression_days 7 -> 30" in result.lines
    assert "config/sources.yaml: selection.historical_min_score 99 -> 15" in result.lines
    assert "config/sources.yaml: selection.topic_relevance_gate_enabled False -> True" in result.lines
    assert "config/sources.yaml: selection.min_topic_relevance_score 1 -> 6" in result.lines
    assert "config/sources.yaml: exports.bibtex_enabled False -> True" in result.lines
    assert "config/sources.yaml: exports.ris_enabled False -> True" in result.lines
    assert "config/sources.yaml: exports.csv_enabled False -> True" in result.lines
    assert "config/sources.yaml: exports.endnote_xml_enabled False -> True" in result.lines
    assert "config/feedback.yaml: events.enabled False -> True" in result.lines
    assert "config/feedback.yaml: events.lookback_days 14 -> 90" in result.lines
    assert "config/feedback.yaml: feishu_comments.enabled False -> True" in result.lines
    assert "config/feedback.yaml: feishu_comments.page_size 10 -> 50" in result.lines
    assert "config/feedback.yaml: feishu_comments.include_replies False -> True" in result.lines
    assert sources_path.read_text(encoding="utf-8") == before
    assert feedback_path.read_text(encoding="utf-8") == feedback_before


def test_enforce_full_profile_write_restores_sources_and_quota(tmp_path, monkeypatch):
    from daily_agent.config import load_config
    from daily_agent.full_profile import enforce_full_profile
    from daily_agent.quality import run_quality_check

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    interests_path = root / "config" / "interests.yaml"
    feedback_path = root / "config" / "feedback.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    interests = yaml.safe_load(interests_path.read_text(encoding="utf-8"))
    feedback = yaml.safe_load(feedback_path.read_text(encoding="utf-8"))
    sources["openalex"]["max_results_per_query"] = 5
    sources["github"]["max_queries_per_domain"] = 2
    sources["google_scholar"]["sort_by_date"] = False
    sources["google_scholar"]["scisbd"] = 0
    sources["google_scholar"]["cite_enrichment_enabled"] = False
    sources["google_scholar"]["cite_enrichment_max_results_per_run"] = 3
    sources["google_scholar"]["cite_cache_enabled"] = False
    sources["google_scholar"]["cite_cache_path"] = "tmp/google_scholar_cites.json"
    sources["google_scholar"]["cite_cache_max_entries"] = 10
    sources["openalex"]["allowed_publication_types"] = []
    sources["semantic_scholar"]["allowed_publication_types"] = []
    sources["crossref"]["allowed_publication_types"] = []
    sources["paper_text"]["max_excerpt_chars"] = 12_000
    sources["citation_discovery"] = {"enabled": False, "max_seed_papers": 2, "max_results_per_seed": 1}
    sources["oa_resolver"] = {"enabled": False, "max_papers_per_run": 5, "run_budget_seconds": 0}
    sources["unpaywall"] = {
        "enabled": False,
        "email_env": "OLD_EMAIL",
        "max_papers_per_run": 5,
        "run_budget_seconds": 0,
        "cache_enabled": False,
        "cache_path": "tmp/unpaywall.json",
        "cache_max_entries": 10,
    }
    sources["pdf_cache"] = {"enabled": False, "max_papers_per_run": 2, "max_pdf_bytes": 8_000_000}
    sources["insights"]["research_gap_enabled"] = False
    sources["selection"]["topic_relevance_gate_enabled"] = False
    sources["exports"] = {"bibtex_enabled": False, "ris_enabled": False, "csv_enabled": False, "endnote_xml_enabled": False}
    interests["quota"]["paper_review_multiplier"] = 1
    feedback["events"]["enabled"] = False
    feedback["feishu_comments"]["enabled"] = False
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")
    interests_path.write_text(yaml.safe_dump(interests, allow_unicode=True, sort_keys=False), encoding="utf-8")
    feedback_path.write_text(yaml.safe_dump(feedback, allow_unicode=True, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.com")

    result = enforce_full_profile(root, write=True)
    config = load_config(root)
    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert result.changed is True
    assert config.sources["github"]["max_queries_per_domain"] == 20
    assert config.sources["github"]["trending_languages"] == ["python", "typescript", "jupyter-notebook", "rust"]
    assert config.sources["google_scholar"]["sort_by_date"] is True
    assert config.sources["google_scholar"]["scisbd"] == 2
    assert config.sources["google_scholar"]["cite_enrichment_enabled"] is True
    assert config.sources["google_scholar"]["cite_enrichment_max_results_per_run"] == 50
    assert config.sources["google_scholar"]["cite_cache_enabled"] is True
    assert config.sources["google_scholar"]["cite_cache_path"] == "data/cache/google_scholar_cites.json"
    assert config.sources["google_scholar"]["cite_cache_max_entries"] == 50_000
    assert config.sources["openalex"]["allowed_publication_types"] == ["article", "review", "preprint", "proceedings-article"]
    assert config.sources["semantic_scholar"]["allowed_publication_types"] == ["JournalArticle", "Conference", "Review", "Preprint"]
    assert config.sources["crossref"]["allowed_publication_types"] == ["journal-article", "proceedings-article", "posted-content"]
    assert config.sources["openalex"]["max_results_per_query"] == 100
    assert config.sources["crossref"]["max_queries_per_domain"] == 8
    assert config.sources["crossref"]["timeout_seconds"] == 60
    assert config.sources["neurips"]["max_detail_pages_per_year"] == 100
    assert config.sources["neurips"]["timeout_seconds"] == 60
    assert config.sources["neurips"]["years"] == [2026, 2025, 2024]
    assert config.sources["paper_text"]["max_excerpt_chars"] == 100_000
    assert config.sources["paper_text"]["run_budget_seconds"] == 180
    assert config.sources["citation_discovery"]["enabled"] is True
    assert config.sources["citation_discovery"]["max_seed_papers"] == 20
    assert config.sources["citation_discovery"]["max_results_per_seed"] == 5
    assert config.sources["oa_resolver"]["enabled"] is True
    assert config.sources["oa_resolver"]["max_papers_per_run"] == 50
    assert config.sources["oa_resolver"]["run_budget_seconds"] == 120
    assert config.sources["unpaywall"]["enabled"] is True
    assert config.sources["unpaywall"]["email_env"] == "UNPAYWALL_EMAIL"
    assert config.sources["unpaywall"]["max_papers_per_run"] == 50
    assert config.sources["unpaywall"]["run_budget_seconds"] == 120
    assert config.sources["unpaywall"]["cache_enabled"] is True
    assert config.sources["unpaywall"]["cache_path"] == "data/cache/unpaywall.json"
    assert config.sources["unpaywall"]["cache_max_entries"] == 50_000
    assert config.sources["pdf_cache"]["enabled"] is True
    assert config.sources["pdf_cache"]["max_papers_per_run"] == 10
    assert config.sources["pdf_cache"]["max_pdf_bytes"] == 30_000_000
    assert config.sources["insights"]["research_gap_enabled"] is True
    assert config.sources["selection"]["topic_relevance_gate_enabled"] is True
    assert config.sources["exports"]["bibtex_enabled"] is True
    assert config.sources["exports"]["ris_enabled"] is True
    assert config.sources["exports"]["csv_enabled"] is True
    assert config.sources["exports"]["endnote_xml_enabled"] is True
    assert config.feedback["events"]["enabled"] is True
    assert config.feedback["feishu_comments"]["enabled"] is True
    assert config.quota["paper_review_multiplier"] == 4
    assert by_key["paper_text"].status == "full"
    assert by_key["oa_resolver"].status == "full"
    assert by_key["unpaywall"].status == "full"
    assert by_key["citation_discovery"].status == "full"
    assert by_key["pdf_cache"].status == "full"
    assert by_key["daily_insights"].status == "full"
    assert by_key["candidate_pool"].status == "full"
    assert by_key["feedback_loop"].status == "full"


def test_quality_enforce_full_cli_and_mcp(tmp_path, monkeypatch, capsys):
    from daily_agent import mcp_server
    from daily_agent.cli import main

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["github"]["trending_enabled"] = False
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    assert main(["quality", "enforce-full", "--root", str(root), "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "Full-profile enforcement: changes pending" in out
    assert "github.trending_enabled False -> True" in out

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.enforce_full_profile("/tmp/daily-agent", write=True) == "exit_code=0"
    assert seen["args"] == ["quality", "enforce-full", "--root", "/tmp/daily-agent", "--write"]


def test_run_cli_enforces_full_profile_before_pipeline(tmp_path, monkeypatch):
    from daily_agent import cli

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["google_scholar"]["enabled"] = False
    sources["paper_text"]["max_papers_per_run"] = 2
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    seen: dict[str, object] = {}

    def fake_run_pipeline(**kwargs):
        current = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
        seen["google_scholar_enabled"] = current["google_scholar"]["enabled"]
        seen["max_papers_per_run"] = current["paper_text"]["max_papers_per_run"]
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            weekly_report_path=root / "reports" / "daily.md",
            weekly_html_path=root / "reports" / "daily.html",
            selected_path=root / "data" / "selected.json",
            items=[],
            status=SimpleNamespace(delivery=None, errors=[]),
            health={},
        )

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    assert cli.main(["run", "--root", str(root), "--date", "2026-07-07", "--dry-run"]) == 0
    assert seen["google_scholar_enabled"] is True
    assert seen["max_papers_per_run"] == 50


def test_quality_and_source_cli_enforce_full_profile_before_checks(tmp_path, monkeypatch):
    from daily_agent import cli
    from daily_agent.source_check import SourceCheckResult
    from daily_agent.quality import QualityProfile

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["openalex"]["max_results_per_query"] = 5
    sources["paper_text"]["max_papers_per_run"] = 2
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    seen: dict[str, object] = {}

    def fake_run_quality_check(config):
        seen["quality_openalex_limit"] = config.sources["openalex"]["max_results_per_query"]
        seen["quality_paper_text_limit"] = config.sources["paper_text"]["max_papers_per_run"]
        return QualityProfile(overall="full", checks=[])

    def fake_run_source_check(config, target_dt, window_days=7, sample_limit=3):
        seen["source_openalex_limit"] = config.sources["openalex"]["max_results_per_query"]
        seen["source_paper_text_limit"] = config.sources["paper_text"]["max_papers_per_run"]
        return [SourceCheckResult(key="openalex", name="OpenAlex", enabled=True, ok=True)]

    monkeypatch.setattr(cli, "run_quality_check", fake_run_quality_check)
    monkeypatch.setattr(cli, "run_source_check", fake_run_source_check)

    assert cli.main(["quality", "check", "--root", str(root)]) == 0
    assert cli.main(["source", "check", "--root", str(root), "--date", "2026-07-07"]) == 0

    assert seen["quality_openalex_limit"] == 100
    assert seen["quality_paper_text_limit"] == 50
    assert seen["source_openalex_limit"] == 100
    assert seen["source_paper_text_limit"] == 50


def test_quality_cli_can_inspect_without_enforcing_full_profile(tmp_path, monkeypatch):
    from daily_agent import cli
    from daily_agent.quality import QualityProfile

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["openalex"]["max_results_per_query"] = 5
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    seen: dict[str, object] = {}

    def fake_run_quality_check(config):
        seen["openalex_limit"] = config.sources["openalex"]["max_results_per_query"]
        return QualityProfile(overall="full", checks=[])

    monkeypatch.setattr(cli, "run_quality_check", fake_run_quality_check)

    assert cli.main(["quality", "check", "--root", str(root), "--no-enforce-full"]) == 0

    assert seen["openalex_limit"] == 5


def test_run_cli_temporarily_enables_supervised_scholar_fallback(tmp_path, monkeypatch):
    import os

    from daily_agent import cli
    from daily_agent.connectors.google_scholar import SCHOLARLY_RUNTIME_ENV

    root = _copy_config(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.delenv(SCHOLARLY_RUNTIME_ENV, raising=False)

    def fake_run_pipeline(**kwargs):
        seen["scholar_env"] = os.environ.get(SCHOLARLY_RUNTIME_ENV)
        return SimpleNamespace(
            weekly_report_path=root / "reports" / "daily.md",
            weekly_html_path=root / "reports" / "daily.html",
            selected_path=root / "data" / "selected.json",
            items=[],
            status=SimpleNamespace(delivery=None, errors=[]),
            health={},
        )

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    assert cli.main(["run", "--root", str(root), "--date", "2026-07-07", "--dry-run", "--supervised-scholar-fallback"]) == 0

    assert seen["scholar_env"] == "1"
    assert os.environ.get(SCHOLARLY_RUNTIME_ENV) is None
