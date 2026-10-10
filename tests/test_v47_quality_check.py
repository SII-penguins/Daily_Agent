from pathlib import Path
import sys

from daily_agent.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_quality_check_reports_full_profile_when_keys_and_dependencies_are_ready(tmp_path, monkeypatch):
    from daily_agent.quality import render_quality_check, run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["llm_writer"]["command"] = "codex"
    config.quota["paper_review_multiplier"] = 4
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 50,
        "max_pdf_bytes": 30_000_000,
        "max_html_bytes": 20_000_000,
        "max_excerpt_chars": 100_000,
        "max_raw_text_chars": 300_000,
        "html_fallback_enabled": True,
        "section_notes_enabled": True,
        "max_section_note_chars": 2400,
        "timeout_seconds": 15,
        "max_urls_per_paper": 4,
        "run_budget_seconds": 180,
    }
    config.sources["selection"] = {"top_candidates_for_llm": 50}
    # The current discovery defaults intentionally retain only an eligible
    # current-year PMLR volume. This synthetic full-profile test must provide
    # its own second eligible-volume premise; do not weaken the legacy gate or
    # reintroduce expired proceedings merely to make a readiness test green.
    config.sources["pmlr"]["volumes"] = [
        {"venue": "Fixture current volume A", "url": "https://example.org/pmlr/a/"},
        {"venue": "Fixture current volume B", "url": "https://example.org/pmlr/b/"},
    ]
    monkeypatch.setenv("SERPAPI_API_KEY", "serpapi-test")
    monkeypatch.setenv("IEEE_XPLORE_API_KEY", "ieee-test")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-test")
    monkeypatch.setenv("CORE_API_KEY", "core-test")
    monkeypatch.setenv("UNPAYWALL_EMAIL", "researcher@example.com")
    monkeypatch.setenv("GITHUB_TOKEN", "github-test")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "feishu-app")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "feishu-secret")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_FOLDER_TOKEN", "feishu-folder")
    versions = {"fitz": "1.28.0", "pypdf": "6.14.2", "scholarly": "1.7.11"}
    monkeypatch.setattr("daily_agent.quality._package_version", lambda name: versions.get(name))
    monkeypatch.setattr("daily_agent.quality._which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert profile.overall == "full"
    assert by_key["python_runtime"].status == "full"
    assert by_key["llm_writer"].status == "full"
    assert by_key["pdf_extractors"].status == "full"
    assert by_key["paper_text"].status == "full"
    assert by_key["oa_resolver"].status == "full"
    assert by_key["unpaywall"].status == "full"
    assert by_key["citation_discovery"].status == "full"
    assert by_key["daily_insights"].status == "full"
    assert by_key["feedback_loop"].status == "full"
    assert by_key["google_scholar"].status == "full"
    assert by_key["core"].status == "full"
    assert by_key["ieee"].status == "full"
    assert by_key["source_coverage"].detail == "13 major sources enabled"

    text = render_quality_check(profile)
    assert "Quality profile: full" in text
    assert f"Python {sys.version_info.major}.{sys.version_info.minor}" in text
    assert "Codex writer full" in text
    assert "Google Scholar full" in text
    assert "CORE full" in text
    assert "PDF text extraction full" in text


def test_quality_check_uses_configured_llm_writer_backend(monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    config.sources["llm_writer"] = {
        "provider": "codex",
        "command": "daily-agent-codex",
    }
    monkeypatch.setattr(
        "daily_agent.quality._which",
        lambda name: "/opt/daily-agent-codex" if name == "daily-agent-codex" else None,
    )

    profile = run_quality_check(config)
    check = {item.key: item for item in profile.checks}["llm_writer"]

    assert check.name == "Codex writer"
    assert check.status == "full"
    assert check.ok is True
    assert "configured command found at /opt/daily-agent-codex" in check.detail
    assert "runtime connectivity" in check.detail


def test_quality_check_rejects_short_llm_writer_budget(monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    config.sources["llm_writer"] = {
        "provider": "codex",
        "command": "codex",
        "timeout_seconds": 90,
        "run_budget_seconds": 180,
    }
    monkeypatch.setattr("daily_agent.quality._which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    check = {item.key: item for item in run_quality_check(config).checks}["llm_writer"]

    assert check.status == "partial"
    assert check.ok is True
    assert "timeout_seconds=90" in check.detail
    assert "run_budget_seconds=180" in check.detail


def test_quality_check_requires_html_fallback_for_full_text_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_pdf_bytes": 20_000_000,
        "max_html_bytes": 10_000_000,
        "max_excerpt_chars": 60_000,
        "max_raw_text_chars": 180_000,
        "html_fallback_enabled": False,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["paper_text"].status == "partial"
    assert "html_fallback_enabled=false" in by_key["paper_text"].detail


def test_quality_check_requires_large_raw_text_scan_budget(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_pdf_bytes": 20_000_000,
        "max_html_bytes": 10_000_000,
        "max_excerpt_chars": 60_000,
        "max_raw_text_chars": 60_000,
        "html_fallback_enabled": True,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["paper_text"].status == "partial"
    assert "max_raw_text_chars=60000" in by_key["paper_text"].detail


def test_quality_check_requires_section_notes_for_full_text_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_pdf_bytes": 20_000_000,
        "max_html_bytes": 10_000_000,
        "max_excerpt_chars": 60_000,
        "max_raw_text_chars": 180_000,
        "html_fallback_enabled": True,
        "section_notes_enabled": False,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["paper_text"].status == "partial"
    assert "section_notes_enabled=false" in by_key["paper_text"].detail


def test_quality_check_rejects_overwide_paper_text_timeout(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_pdf_bytes": 20_000_000,
        "max_html_bytes": 10_000_000,
        "max_excerpt_chars": 60_000,
        "max_raw_text_chars": 180_000,
        "html_fallback_enabled": True,
        "section_notes_enabled": True,
        "max_section_note_chars": 1600,
        "timeout_seconds": 45,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["paper_text"].status == "partial"
    assert "timeout_seconds=45" in by_key["paper_text"].detail


def test_quality_check_requires_paper_text_runtime_budgets(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_pdf_bytes": 20_000_000,
        "max_html_bytes": 10_000_000,
        "max_excerpt_chars": 60_000,
        "max_raw_text_chars": 180_000,
        "html_fallback_enabled": True,
        "section_notes_enabled": True,
        "max_section_note_chars": 1600,
        "timeout_seconds": 15,
        "max_urls_per_paper": 8,
        "run_budget_seconds": 0,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["paper_text"].status == "partial"
    assert "max_urls_per_paper=8" in by_key["paper_text"].detail
    assert "run_budget_seconds=0" in by_key["paper_text"].detail


def test_quality_check_requires_pdf_cache_full_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["pdf_cache"] = {
        "enabled": False,
        "max_papers_per_run": 2,
        "max_pdf_bytes": 8_000_000,
        "max_urls_per_paper": 1,
        "run_budget_seconds": 0,
    }

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["pdf_cache"].status == "disabled"
    assert "enabled=false" in by_key["pdf_cache"].detail
    assert "max_papers_per_run=2" in by_key["pdf_cache"].detail
    assert "max_pdf_bytes=8000000" in by_key["pdf_cache"].detail
    assert "max_urls_per_paper=1" in by_key["pdf_cache"].detail
    assert "run_budget_seconds=0" in by_key["pdf_cache"].detail


def test_quality_check_requires_feedback_loop_full_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.feedback["events"]["enabled"] = False
    config.feedback["events"]["lookback_days"] = 14
    config.feedback["feishu_comments"] = {"enabled": False, "page_size": 10, "include_replies": False}

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["feedback_loop"].status == "partial"
    assert "events.enabled=false" in by_key["feedback_loop"].detail
    assert "lookback_days=14" in by_key["feedback_loop"].detail
    assert "feishu_comments.enabled=false" in by_key["feedback_loop"].detail


def test_quality_check_requires_bibtex_export(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["exports"] = {"bibtex_enabled": False, "ris_enabled": False, "csv_enabled": False, "endnote_xml_enabled": False}

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["exports"].status == "partial"
    assert "bibtex_enabled=false" in by_key["exports"].detail
    assert "ris_enabled=false" in by_key["exports"].detail
    assert "csv_enabled=false" in by_key["exports"].detail
    assert "endnote_xml_enabled=false" in by_key["exports"].detail


def test_quality_check_requires_high_recall_source_limits(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["openalex"]["max_results_per_query"] = 5
    config.sources["crossref"]["timeout_seconds"] = 45
    config.sources["github"]["trending_enabled"] = False
    config.sources["github"]["trending_languages"] = ["python"]
    config.sources["github"]["max_queries_per_domain"] = 2
    config.sources["google_scholar"]["source_check_timeout_seconds"] = 120
    config.sources["google_scholar"]["scholarly_runtime_timeout_seconds"] = 999
    config.sources["google_scholar"]["sort_by_date"] = False
    config.sources["google_scholar"]["scisbd"] = 0

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["source_limits"].status == "partial"
    assert "github.max_queries_per_domain=2 < 20" in by_key["source_limits"].detail
    assert "openalex.max_results_per_query=5 < 100" in by_key["source_limits"].detail
    assert "crossref.timeout_seconds=45 < 60" in by_key["source_limits"].detail
    assert "github.trending_enabled=false" in by_key["source_limits"].detail
    assert "github.trending_languages_count=1 < 4" in by_key["source_limits"].detail
    assert "google_scholar.source_check_timeout_seconds=120 > 30" in by_key["source_limits"].detail
    assert "google_scholar.scholarly_runtime_timeout_seconds=999 > 120" in by_key["source_limits"].detail
    assert "google_scholar.sort_by_date=false" in by_key["source_limits"].detail
    assert "google_scholar.scisbd=0 != 2" in by_key["source_limits"].detail


def test_quality_check_requires_publication_type_filters(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["openalex"]["allowed_publication_types"] = ["article"]
    config.sources["semantic_scholar"].pop("allowed_publication_types", None)
    config.sources["crossref"]["allowed_publication_types"] = []

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["source_limits"].status == "partial"
    assert "openalex.allowed_publication_types_count=1 < 3" in by_key["source_limits"].detail
    assert "semantic_scholar.allowed_publication_types_count=0 < 3" in by_key["source_limits"].detail
    assert "crossref.allowed_publication_types_count=0 < 3" in by_key["source_limits"].detail


def test_quality_check_marks_google_scholar_fallback_without_serpapi(tmp_path, monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["google_scholar"] = {
        "enabled": True,
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": True,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setattr("daily_agent.quality.scholarly_available", lambda: True)

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["google_scholar"].ok is True
    assert by_key["google_scholar"].status == "fallback"
    assert "scholarly fallback" in by_key["google_scholar"].detail


def test_quality_check_marks_google_scholar_fallback_with_supervised_env_override(tmp_path, monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["google_scholar"] = {
        "enabled": True,
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": False,
    }
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")
    monkeypatch.setenv("DAILY_AGENT_SUPERVISED_SCHOLARLY_RUNTIME", "1")
    monkeypatch.setattr("daily_agent.quality.scholarly_available", lambda: True)

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["google_scholar"].ok is True
    assert by_key["google_scholar"].status == "fallback"
    assert "supervised scholarly fallback available" in by_key["google_scholar"].detail


def test_quality_check_cli_prints_report(tmp_path, monkeypatch, capsys):
    from daily_agent.cli import main
    from daily_agent.quality import QualityCheck, QualityProfile

    profile = QualityProfile(
        overall="full",
        checks=[QualityCheck(key="llm_writer", name="Codex writer", status="full", ok=True, detail="codex found")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config(ROOT))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)

    assert main(["quality", "check", "--root", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "Quality profile: full" in out
    assert "Codex writer full codex found" in out


def test_quality_check_cli_require_full_fails_when_profile_is_degraded(tmp_path, monkeypatch, capsys):
    from daily_agent.cli import main
    from daily_agent.quality import QualityCheck, QualityProfile

    profile = QualityProfile(
        overall="degraded",
        checks=[QualityCheck(key="ieee", name="IEEE Xplore", status="missing", ok=False, detail="missing IEEE_XPLORE_API_KEY")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config(ROOT))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)

    assert main(["quality", "check", "--root", str(tmp_path), "--require-full"]) == 2

    out = capsys.readouterr().out
    assert "Quality profile: degraded" in out
    assert "Required full-quality profile but got degraded" in out


def test_mcp_quality_check_invokes_quality_cli(monkeypatch):
    from daily_agent import mcp_server

    seen = {}
    def fake_run_cli(args):
        seen["args"] = args
        return "ok"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.quality_check("/tmp/daily-agent")

    assert output == "ok"
    assert seen["args"] == ["quality", "check", "--root", "/tmp/daily-agent", "--require-full"]


def test_mcp_quality_check_can_require_full(monkeypatch):
    from daily_agent import mcp_server

    seen = {}
    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=2"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.quality_check("/tmp/daily-agent", require_full=True)

    assert output == "exit_code=2"
    assert seen["args"] == ["quality", "check", "--root", "/tmp/daily-agent", "--require-full"]


def test_mcp_quality_check_can_report_without_requiring_full(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.quality_check("/tmp/daily-agent", require_full=False)

    assert output == "exit_code=0"
    assert seen["args"] == ["quality", "check", "--root", "/tmp/daily-agent"]


def test_mcp_quality_check_can_bypass_full_profile_guard(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.quality_check("/tmp/daily-agent", enforce_full=False)

    assert output == "exit_code=0"
    assert seen["args"] == ["quality", "check", "--root", "/tmp/daily-agent", "--require-full", "--no-enforce-full"]


def test_current_single_volume_default_keeps_conservative_quality_warning():
    from daily_agent.quality import run_quality_check
    config = load_config(ROOT)
    assert len(config.sources["pmlr"]["volumes"]) == 1
    check = {item.key: item for item in run_quality_check(config).checks}["source_limits"]
    assert check.status == "partial"
    assert "pmlr.volumes_count=1 < 2" in check.detail
