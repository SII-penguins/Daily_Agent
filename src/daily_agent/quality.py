from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import sys
from dataclasses import dataclass

from daily_agent.config import AppConfig
from daily_agent.connectors import scholarly_available
from daily_agent.connectors.google_scholar import scholarly_runtime_enabled
from daily_agent.secrets import credential_value


@dataclass(frozen=True)
class QualityCheck:
    key: str
    name: str
    status: str
    ok: bool
    detail: str
    action: str | None = None


@dataclass(frozen=True)
class QualityProfile:
    overall: str
    checks: list[QualityCheck]


def run_quality_check(config: AppConfig) -> QualityProfile:
    checks = [
        _check_python_runtime(),
        _check_llm_writer(config),
        _check_pdf_extractors(),
        _check_paper_text(config),
        _check_oa_resolver(config),
        _check_unpaywall(config),
        _check_pdf_cache(config),
        _check_citation_context(config),
        _check_citation_discovery(config),
        _check_query_expansion(config),
        _check_daily_insights(config),
        _check_exports(config),
        _check_feedback_loop(config),
        _check_candidate_pool(config),
        _check_source_coverage(config),
        _check_source_limits(config),
        _check_conference_sources(config),
        _check_google_scholar(config),
        _check_semantic_scholar(config),
        _check_core(config),
        _check_ieee(config),
        _check_github(config),
        _check_feishu(config),
        _check_schedule(config),
    ]
    return QualityProfile(overall=_overall(checks), checks=checks)


def render_quality_check(profile: QualityProfile) -> str:
    lines = [f"Quality profile: {profile.overall}"]
    for check in profile.checks:
        lines.append(f"- {check.name} {check.status} {check.detail}")
        if check.action:
            lines.append(f"  action: {check.action}")
    return "\n".join(lines)


def _overall(checks: list[QualityCheck]) -> str:
    if all(check.status == "full" for check in checks):
        return "full"
    if any(not check.ok for check in checks):
        return "degraded"
    return "fallback"


def _check_llm_writer(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("llm_writer", {}) or {}
    provider = str(source_config.get("provider") or "codex").strip().lower()
    command = str(source_config.get("command") or provider).strip()
    timeout_seconds = float(source_config.get("timeout_seconds", 600) or 0)
    run_budget_seconds = float(source_config.get("run_budget_seconds", 14_400) or 0)
    batch_size = int(source_config.get("batch_size", 2) or 0)
    name = "Codex writer"
    if provider != "codex":
        return QualityCheck(
            "llm_writer",
            name,
            "missing",
            False,
            f"unsupported internal llm_writer.provider={provider or '<empty>'}; Daily Agent writing is Codex-only",
            "Set llm_writer.provider to codex and configure the codex command on PATH.",
        )

    path = _which(command)
    if path:
        detail = (
            f"configured command found at {path}; timeout_seconds={timeout_seconds:g}, "
            f"run_budget_seconds={run_budget_seconds:g}, batch_size={batch_size}; "
            "runtime connectivity is verified by each bounded drafting call"
        )
        if timeout_seconds < 300 or run_budget_seconds < 3_600:
            return QualityCheck(
                "llm_writer",
                name,
                "partial",
                True,
                detail,
                "Use timeout_seconds>=300 and run_budget_seconds>=3600 for unattended full-text drafting.",
            )
        return QualityCheck(
            "llm_writer",
            name,
            "full",
            True,
            detail,
        )
    return QualityCheck(
        "llm_writer",
        name,
        "missing",
        False,
        f"configured command {command!r} not found; drafting will use the full-text rule fallback",
        "Install/configure the codex CLI on PATH, or set llm_writer.command to its absolute path.",
    )


def _check_python_runtime() -> QualityCheck:
    version = sys.version_info
    detail = f"Python {version.major}.{version.minor}.{version.micro} at {sys.executable}"
    if (version.major, version.minor) >= (3, 11):
        return QualityCheck("python_runtime", "Python runtime", "full", True, detail)
    return QualityCheck(
        "python_runtime",
        "Python runtime",
        "missing",
        False,
        f"{detail}; Daily Agent requires Python 3.11+",
        "Run Daily Agent through a Python 3.11+ environment such as the project venv or Anaconda interpreter.",
    )


def _check_pdf_extractors() -> QualityCheck:
    fitz_version = _package_version("fitz")
    pypdf_version = _package_version("pypdf")
    details = []
    if fitz_version:
        details.append(f"PyMuPDF={fitz_version}")
    if pypdf_version:
        details.append(f"pypdf={pypdf_version}")
    if fitz_version and pypdf_version:
        return QualityCheck("pdf_extractors", "PDF text extraction", "full", True, ", ".join(details))
    if fitz_version or pypdf_version:
        return QualityCheck(
            "pdf_extractors",
            "PDF text extraction",
            "partial",
            True,
            ", ".join(details),
            'Install both extractors with pip install -e ".[pdf]" for more robust PDF parsing.',
        )
    return QualityCheck(
        "pdf_extractors",
        "PDF text extraction",
        "missing",
        False,
        "PyMuPDF and pypdf are unavailable; only the lightweight stream parser can run",
        'Install full dependencies with pip install -e ".[full,test]".',
    )


def _check_paper_text(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("paper_text", {}) or {}
    if not source_config.get("enabled", True):
        return QualityCheck("paper_text", "Full-text enrichment", "disabled", False, "paper_text.enabled is false")
    max_papers = int(source_config.get("max_papers_per_run", 7))
    max_chars = int(source_config.get("max_excerpt_chars", 12_000))
    max_bytes = int(source_config.get("max_pdf_bytes", 8_000_000))
    max_html_bytes = int(source_config.get("max_html_bytes", max_bytes))
    max_raw_chars = int(source_config.get("max_raw_text_chars", max_chars * 3))
    html_fallback = bool(source_config.get("html_fallback_enabled", True))
    section_notes_enabled = bool(source_config.get("section_notes_enabled", True))
    max_section_note_chars = int(source_config.get("max_section_note_chars", 1200))
    timeout = float(source_config.get("timeout_seconds", 0))
    max_urls_per_paper = int(source_config.get("max_urls_per_paper", 0))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0))
    detail = (
        f"max_papers={max_papers}, max_pdf_bytes={max_bytes}, max_html_bytes={max_html_bytes}, "
        f"max_excerpt_chars={max_chars}, max_raw_text_chars={max_raw_chars}, "
        f"html_fallback_enabled={str(html_fallback).lower()}, "
        f"section_notes_enabled={str(section_notes_enabled).lower()}, max_section_note_chars={max_section_note_chars}, "
        f"timeout_seconds={timeout:g}, max_urls_per_paper={max_urls_per_paper}, "
        f"run_budget_seconds={run_budget_seconds:g}"
    )
    if (
        max_papers >= 50
        and max_chars >= 100_000
        and max_raw_chars >= max_chars * 3
        and max_bytes >= 30_000_000
        and max_html_bytes >= 20_000_000
        and html_fallback
        and section_notes_enabled
        and max_section_note_chars >= 2400
        and 10 <= timeout <= 20
        and max_urls_per_paper == 4
        and run_budget_seconds >= 180
    ):
        return QualityCheck("paper_text", "Full-text enrichment", "full", True, detail)
    if max_papers > 0 and max_chars >= 12_000:
        return QualityCheck(
            "paper_text",
            "Full-text enrichment",
            "partial",
            True,
            detail,
            "Use the full profile: paper_text.max_papers_per_run>=50, max_excerpt_chars>=100000, max_raw_text_chars>=300000, max_pdf_bytes>=30000000, max_html_bytes>=20000000, max_urls_per_paper=4, run_budget_seconds>=180, and section_notes_enabled=true.",
        )
    return QualityCheck(
        "paper_text",
        "Full-text enrichment",
        "missing",
        False,
        detail,
        "Enable paper_text and give the writer enough excerpt budget to inspect method/results/limitations.",
    )


def _check_oa_resolver(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("oa_resolver", {}) or {}
    enabled = bool(source_config.get("enabled", False))
    max_papers = int(source_config.get("max_papers_per_run", 0))
    timeout = float(source_config.get("timeout_seconds", 0))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    detail = (
        f"enabled={str(enabled).lower()}, max_papers_per_run={max_papers}, "
        f"timeout_seconds={timeout:g}, run_budget_seconds={run_budget_seconds:g}"
    )
    if enabled and max_papers >= 50 and 10 <= timeout <= 60 and run_budget_seconds >= 120:
        return QualityCheck("oa_resolver", "Open-access link resolver", "full", True, detail)
    if enabled:
        return QualityCheck(
            "oa_resolver",
            "Open-access link resolver",
            "partial",
            True,
            detail,
            "Use oa_resolver.max_papers_per_run>=50, timeout_seconds between 10 and 60, and run_budget_seconds>=120 so DOI-only papers get OA PDF/HTML candidates before full-text extraction.",
        )
    return QualityCheck(
        "oa_resolver",
        "Open-access link resolver",
        "disabled",
        False,
        detail,
        "Enable oa_resolver so Daily Agent can resolve OpenAlex open-access PDFs and landing pages before full-text enrichment.",
    )


def _check_unpaywall(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("unpaywall", {}) or {}
    enabled = bool(source_config.get("enabled", False))
    email_env = str(source_config.get("email_env") or "UNPAYWALL_EMAIL")
    email_present = bool(credential_value(email_env))
    max_papers = int(source_config.get("max_papers_per_run", 0))
    timeout = float(source_config.get("timeout_seconds", 0))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    cache_enabled = bool(source_config.get("cache_enabled", False))
    cache_path = str(source_config.get("cache_path") or "")
    cache_max_entries = int(source_config.get("cache_max_entries", 0) or 0)
    detail = (
        f"enabled={str(enabled).lower()}, email_env={email_env}, email_present={str(email_present).lower()}, "
        f"max_papers_per_run={max_papers}, timeout_seconds={timeout:g}, run_budget_seconds={run_budget_seconds:g}, "
        f"cache_enabled={str(cache_enabled).lower()}, cache_path={cache_path or '<missing>'}, "
        f"cache_max_entries={cache_max_entries}"
    )
    if (
        enabled
        and email_present
        and max_papers >= 50
        and 10 <= timeout <= 60
        and run_budget_seconds >= 120
        and cache_enabled
        and bool(cache_path)
        and cache_max_entries >= 50_000
    ):
        return QualityCheck("unpaywall", "Unpaywall OA resolver", "full", True, detail)
    if enabled:
        return QualityCheck(
            "unpaywall",
            "Unpaywall OA resolver",
            "missing" if not email_present else "partial",
            email_present,
            detail,
            "Set UNPAYWALL_EMAIL and use unpaywall.max_papers_per_run>=50, timeout_seconds between 10 and 60, run_budget_seconds>=120, cache_enabled=true, and cache_max_entries>=50000 so DOI-only papers get another OA PDF/HTML resolver before full-text extraction.",
        )
    return QualityCheck(
        "unpaywall",
        "Unpaywall OA resolver",
        "disabled",
        False,
        detail,
        "Enable unpaywall and set UNPAYWALL_EMAIL so Daily Agent can resolve DOI-based open-access PDFs and landing pages.",
    )


def _check_pdf_cache(config: AppConfig) -> QualityCheck:
    cache_config = config.sources.get("pdf_cache", {}) or {}
    enabled = bool(cache_config.get("enabled", False))
    max_papers = int(cache_config.get("max_papers_per_run", 0))
    max_bytes = int(cache_config.get("max_pdf_bytes", 0))
    max_urls = int(cache_config.get("max_urls_per_paper", 0))
    timeout = float(cache_config.get("timeout_seconds", 0))
    run_budget_seconds = float(cache_config.get("run_budget_seconds", 0) or 0)
    output_dir = str(cache_config.get("output_dir") or "")
    detail = (
        f"enabled={str(enabled).lower()}, max_papers_per_run={max_papers}, "
        f"max_pdf_bytes={max_bytes}, max_urls_per_paper={max_urls}, "
        f"timeout_seconds={timeout:g}, run_budget_seconds={run_budget_seconds:g}, output_dir={output_dir or '<missing>'}"
    )
    if (
        enabled
        and max_papers >= 10
        and max_bytes >= 30_000_000
        and max_urls == 4
        and 10 <= timeout <= 30
        and run_budget_seconds >= 180
        and bool(output_dir)
    ):
        return QualityCheck("pdf_cache", "Selected PDF cache", "full", True, detail)
    if enabled:
        return QualityCheck(
            "pdf_cache",
            "Selected PDF cache",
            "partial",
            True,
            detail,
            "Use pdf_cache.max_papers_per_run>=10, max_pdf_bytes>=30000000, max_urls_per_paper=4, timeout_seconds between 10 and 30, run_budget_seconds>=180, and output_dir=data/pdfs.",
        )
    return QualityCheck(
        "pdf_cache",
        "Selected PDF cache",
        "disabled",
        False,
        detail,
        "Enable pdf_cache so approved papers with accessible OA PDFs are saved locally for later deep reading.",
    )


def _check_candidate_pool(config: AppConfig) -> QualityCheck:
    selection = config.sources.get("selection", {}) or {}
    top_candidates = int(selection.get("top_candidates_for_llm", 15))
    paper_multiplier = int(config.quota.get("paper_review_multiplier", 2))
    topic_gate = bool(selection.get("topic_relevance_gate_enabled", True))
    min_relevance = float(selection.get("min_topic_relevance_score", 6.0))
    off_topic_penalty = float(selection.get("off_topic_score_penalty", -120.0))
    detail = (
        f"top_candidates_for_llm={top_candidates}, paper_review_multiplier={paper_multiplier}, "
        f"topic_relevance_gate_enabled={str(topic_gate).lower()}, "
        f"min_topic_relevance_score={min_relevance:g}, off_topic_score_penalty={off_topic_penalty:g}"
    )
    if top_candidates >= 50 and paper_multiplier >= 4 and topic_gate and min_relevance >= 6 and off_topic_penalty <= -80:
        return QualityCheck("candidate_pool", "LLM review pool", "full", True, detail)
    return QualityCheck(
        "candidate_pool",
        "LLM review pool",
        "partial",
        True,
        detail,
        "Use top_candidates_for_llm>=50, paper_review_multiplier>=4, and enable the topic relevance gate to reduce shallow/off-topic shortlist bias.",
    )


def _check_citation_context(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("citation_context", {}) or {}
    enabled = bool(source_config.get("enabled", True))
    max_papers = int(source_config.get("max_papers_per_run", 0))
    max_citing = int(source_config.get("max_citing_papers", 0))
    max_referenced = int(source_config.get("max_referenced_papers", 0))
    timeout = float(source_config.get("timeout_seconds", 0))
    detail = (
        f"enabled={str(enabled).lower()}, max_papers_per_run={max_papers}, "
        f"max_citing_papers={max_citing}, max_referenced_papers={max_referenced}, timeout_seconds={timeout:g}"
    )
    if enabled and max_papers >= 30 and max_citing >= 8 and max_referenced >= 8 and timeout >= 60:
        return QualityCheck("citation_context", "Citation context enrichment", "full", True, detail)
    if enabled and max_papers > 0:
        return QualityCheck(
            "citation_context",
            "Citation context enrichment",
            "partial",
            True,
            detail,
            "Use citation_context.max_papers_per_run>=30, max_citing_papers>=8, max_referenced_papers>=8, and timeout_seconds>=60.",
        )
    return QualityCheck(
        "citation_context",
        "Citation context enrichment",
        "disabled",
        False,
        detail,
        "Enable citation_context so Daily Agent can explain upstream references and downstream citing papers.",
    )


def _check_citation_discovery(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("citation_discovery", {}) or {}
    enabled = bool(source_config.get("enabled", False))
    recent_days = int(source_config.get("recent_days", 0))
    max_seed_papers = int(source_config.get("max_seed_papers", 0))
    max_results_per_seed = int(source_config.get("max_results_per_seed", 0))
    timeout = float(source_config.get("timeout_seconds", 0))
    detail = (
        f"enabled={str(enabled).lower()}, recent_days={recent_days}, "
        f"max_seed_papers={max_seed_papers}, max_results_per_seed={max_results_per_seed}, timeout_seconds={timeout:g}"
    )
    if enabled and recent_days >= 30 and max_seed_papers >= 20 and max_results_per_seed >= 5 and timeout >= 60:
        return QualityCheck("citation_discovery", "Citation-neighborhood discovery", "full", True, detail)
    if enabled:
        return QualityCheck(
            "citation_discovery",
            "Citation-neighborhood discovery",
            "partial",
            True,
            detail,
            "Use citation_discovery.recent_days>=30, max_seed_papers>=20, max_results_per_seed>=5, and timeout_seconds>=60.",
        )
    return QualityCheck(
        "citation_discovery",
        "Citation-neighborhood discovery",
        "disabled",
        False,
        detail,
        "Enable citation_discovery so Daily Agent can discover new papers that cite your previously selected or high-scoring papers.",
    )


def _check_daily_insights(config: AppConfig) -> QualityCheck:
    insight_config = config.sources.get("insights", {}) or {}
    enabled = bool(insight_config.get("enabled", True))
    include_in_reports = bool(insight_config.get("include_in_reports", True))
    max_insights = int(insight_config.get("max_insights", 3))
    min_items = int(insight_config.get("min_items", 2))
    research_gap_enabled = bool(insight_config.get("research_gap_enabled", True))
    detail = (
        f"enabled={str(enabled).lower()}, include_in_reports={str(include_in_reports).lower()}, "
        f"max_insights={max_insights}, min_items={min_items}, research_gap_enabled={str(research_gap_enabled).lower()}"
    )
    if enabled and include_in_reports and max_insights >= 5 and min_items <= 2 and research_gap_enabled:
        return QualityCheck("daily_insights", "Daily insight synthesis", "full", True, detail)
    if enabled and include_in_reports and max_insights > 0:
        return QualityCheck(
            "daily_insights",
            "Daily insight synthesis",
            "partial",
            True,
            detail,
            "Use insights.enabled=true, include_in_reports=true, max_insights>=5, min_items<=2, and research_gap_enabled=true for the full cross-item insight layer.",
        )
    return QualityCheck(
        "daily_insights",
        "Daily insight synthesis",
        "disabled",
        False,
        detail,
        "Enable the Daily Insights report layer so the digest includes trend/method/follow-up synthesis.",
    )


def _check_exports(config: AppConfig) -> QualityCheck:
    export_config = config.sources.get("exports", {}) or {}
    bibtex_enabled = bool(export_config.get("bibtex_enabled", False))
    ris_enabled = bool(export_config.get("ris_enabled", False))
    csv_enabled = bool(export_config.get("csv_enabled", False))
    endnote_xml_enabled = bool(export_config.get("endnote_xml_enabled", False))
    detail = (
        f"bibtex_enabled={str(bibtex_enabled).lower()}, "
        f"ris_enabled={str(ris_enabled).lower()}, csv_enabled={str(csv_enabled).lower()}, "
        f"endnote_xml_enabled={str(endnote_xml_enabled).lower()}"
    )
    if bibtex_enabled and ris_enabled and csv_enabled and endnote_xml_enabled:
        return QualityCheck("exports", "Paper exports", "full", True, detail)
    return QualityCheck(
        "exports",
        "Paper exports",
        "partial",
        True,
        detail,
        "Enable exports.bibtex_enabled, exports.ris_enabled, exports.csv_enabled, and exports.endnote_xml_enabled so each digest writes BibTeX, RIS, CSV, and EndNote XML sidecars.",
    )


def _check_query_expansion(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("query_expansion", {}) or {}
    enabled = bool(source_config.get("enabled", False))
    max_terms = int(source_config.get("max_expand_terms_per_domain", 0))
    configured_domains = sum(1 for domain in config.domains if getattr(domain, "expanded_keywords", []))
    detail = f"enabled={str(enabled).lower()}, max_expand_terms_per_domain={max_terms}, configured_domains={configured_domains}"
    if enabled and max_terms >= 16 and configured_domains >= 1:
        return QualityCheck("query_expansion", "Query expansion", "full", True, detail)
    if enabled and max_terms > 0:
        return QualityCheck(
            "query_expansion",
            "Query expansion",
            "partial",
            True,
            detail,
            "Use query_expansion.enabled=true, max_expand_terms_per_domain>=16, and add keywords.expand terms in config/interests.yaml.",
        )
    return QualityCheck(
        "query_expansion",
        "Query expansion",
        "disabled",
        False,
        detail,
        "Enable query_expansion so Daily Agent searches related terms instead of only exact seed keywords.",
    )


def _check_feedback_loop(config: AppConfig) -> QualityCheck:
    feedback = config.feedback or {}
    events = feedback.get("events", {}) or {}
    feishu_comments = feedback.get("feishu_comments", {}) or {}
    events_enabled = bool(events.get("enabled", False))
    lookback_days = int(events.get("lookback_days", 0) or 0)
    half_life_days = float(events.get("half_life_days", 0) or 0)
    max_score = float(events.get("max_feedback_score", 0) or 0)
    min_score = float(events.get("min_feedback_score", 0) or 0)
    feishu_enabled = bool(feishu_comments.get("enabled", False))
    page_size = int(feishu_comments.get("page_size", 0) or 0)
    include_replies = bool(feishu_comments.get("include_replies", False))
    detail = (
        f"events.enabled={str(events_enabled).lower()}, lookback_days={lookback_days}, "
        f"half_life_days={half_life_days:g}, max_feedback_score={max_score:g}, "
        f"min_feedback_score={min_score:g}, feishu_comments.enabled={str(feishu_enabled).lower()}, "
        f"page_size={page_size}, include_replies={str(include_replies).lower()}"
    )
    if events_enabled and lookback_days >= 90 and 1 <= half_life_days <= 30 and max_score >= 12 and min_score <= -15 and feishu_enabled and page_size >= 50 and include_replies:
        return QualityCheck("feedback_loop", "Feedback loop", "full", True, detail)
    return QualityCheck(
        "feedback_loop",
        "Feedback loop",
        "partial",
        True,
        detail,
        "Use the full profile: feedback events enabled with 90-day lookback/30-day half-life, score caps enabled, and Feishu comment replies enabled for feedback sync.",
    )


def _check_source_coverage(config: AppConfig) -> QualityCheck:
    expected = ["arxiv", "github", "openalex", "semantic_scholar", "citation_discovery", "google_scholar", "crossref", "core", "dblp", "ieee", "openreview", "pmlr", "neurips"]
    missing = [key for key in expected if not _source_enabled(config, key)]
    if not missing:
        return QualityCheck("source_coverage", "Source coverage", "full", True, f"{len(expected)} major sources enabled")
    return QualityCheck(
        "source_coverage",
        "Source coverage",
        "partial",
        True,
        f"disabled: {', '.join(missing)}",
        "Enable missing sources in config/sources.yaml if they match your compliance/API constraints.",
    )


def _check_source_limits(config: AppConfig) -> QualityCheck:
    issues: list[str] = []
    _require_min_int(config.sources, "arxiv", "max_results_per_query", 100, issues)
    _require_min_int(config.sources, "arxiv", "max_queries_per_domain", 8, issues)
    _require_max_int(config.sources, "arxiv", "max_queries_per_domain", 12, issues)
    _require_min_number(config.sources, "arxiv", "request_delay_seconds", 3, issues)

    _require_bool(config.sources, "github", "search_enabled", True, issues)
    _require_bool(config.sources, "github", "trending_enabled", True, issues)
    _require_min_int(config.sources, "github", "normal_active_days", 30, issues)
    _require_min_int(config.sources, "github", "high_relevance_active_days", 90, issues)
    _require_min_int(config.sources, "github", "max_results_per_query", 50, issues)
    _require_min_int(config.sources, "github", "max_queries_per_domain", 20, issues)
    _require_max_int(config.sources, "github", "max_queries_per_domain", 32, issues)
    _require_min_int(config.sources, "github", "update_signal_max_repos", 50, issues)
    _require_max_int(config.sources, "github", "min_stars", 0, issues)
    _require_min_len(config.sources, "github", "trending_languages", 4, issues)

    for source in ["openalex", "semantic_scholar", "core", "dblp", "ieee"]:
        _require_min_int(config.sources, source, "max_results_per_query", 100, issues)
        _require_min_int(config.sources, source, "max_queries_per_domain", 8, issues)
        _require_max_int(config.sources, source, "max_queries_per_domain", 12, issues)
        _require_min_number(config.sources, source, "timeout_seconds", 60, issues)
    for source in ["openalex", "semantic_scholar", "crossref"]:
        _require_min_len(config.sources, source, "allowed_publication_types", 3, issues)
    _require_min_int(config.sources, "crossref", "max_results_per_query", 100, issues)
    _require_min_int(config.sources, "crossref", "max_queries_per_domain", 8, issues)
    _require_max_int(config.sources, "crossref", "max_queries_per_domain", 12, issues)
    _require_min_number(config.sources, "crossref", "timeout_seconds", 60, issues)
    _require_min_int(config.sources, "dblp", "recent_years", 2, issues)

    _require_min_int(config.sources, "google_scholar", "recent_days", 30, issues)
    _require_min_int(config.sources, "google_scholar", "max_results_per_query", 40, issues)
    _require_min_int(config.sources, "google_scholar", "max_queries_per_domain", 8, issues)
    _require_max_int(config.sources, "google_scholar", "max_queries_per_domain", 12, issues)
    _require_min_int(config.sources, "google_scholar", "scholarly_max_results_per_query", 20, issues)
    _require_min_number(config.sources, "google_scholar", "source_check_timeout_seconds", 5, issues)
    _require_max_number(config.sources, "google_scholar", "source_check_timeout_seconds", 30, issues)
    _require_min_number(config.sources, "google_scholar", "scholarly_runtime_timeout_seconds", 15, issues)
    _require_max_number(config.sources, "google_scholar", "scholarly_runtime_timeout_seconds", 120, issues)
    _require_min_number(config.sources, "google_scholar", "timeout_seconds", 60, issues)
    _require_bool(config.sources, "google_scholar", "scholarly_fallback_enabled", True, issues)
    _require_bool(config.sources, "google_scholar", "scholarly_runtime_enabled", False, issues)
    _require_bool(config.sources, "google_scholar", "sort_by_date", True, issues)
    _require_exact_int(config.sources, "google_scholar", "scisbd", 2, issues)
    _require_bool(config.sources, "google_scholar", "cite_enrichment_enabled", True, issues)
    _require_min_int(config.sources, "google_scholar", "cite_enrichment_max_results_per_run", 50, issues)
    _require_min_number(config.sources, "google_scholar", "cite_enrichment_run_budget_seconds", 15, issues)
    _require_bool(config.sources, "google_scholar", "cite_cache_enabled", True, issues)
    _require_exact_str(config.sources, "google_scholar", "cite_cache_path", "data/cache/google_scholar_cites.json", issues)
    _require_min_int(config.sources, "google_scholar", "cite_cache_max_entries", 50_000, issues)

    _require_min_int(config.sources, "openreview", "recent_days", 30, issues)
    _require_min_int(config.sources, "openreview", "max_results_per_venue", 100, issues)
    _require_min_number(config.sources, "openreview", "timeout_seconds", 60, issues)
    _require_min_len(config.sources, "openreview", "venues", 2, issues)

    _require_min_int(config.sources, "pmlr", "recent_days", 365, issues)
    _require_min_int(config.sources, "pmlr", "max_results_per_volume", 100, issues)
    _require_min_number(config.sources, "pmlr", "timeout_seconds", 60, issues)
    _require_min_len(config.sources, "pmlr", "volumes", 2, issues)

    _require_min_int(config.sources, "neurips", "recent_days", 365, issues)
    _require_min_int(config.sources, "neurips", "max_results_per_year", 100, issues)
    _require_min_int(config.sources, "neurips", "max_detail_pages_per_year", 100, issues)
    _require_min_number(config.sources, "neurips", "timeout_seconds", 60, issues)
    _require_min_len(config.sources, "neurips", "years", 3, issues)

    if not issues:
        return QualityCheck("source_limits", "Source high-recall limits", "full", True, "all source limits meet full profile thresholds")
    return QualityCheck(
        "source_limits",
        "Source high-recall limits",
        "partial",
        True,
        "below target: " + "; ".join(issues[:12]),
        "Restore config/sources.yaml high-recall limits before formal full-quality delivery.",
    )


def _check_conference_sources(config: AppConfig) -> QualityCheck:
    missing = [key for key in ["openreview", "pmlr", "neurips"] if not _source_enabled(config, key)]
    if not missing:
        return QualityCheck("conference_sources", "ICLR/ICML/NeurIPS coverage", "full", True, "OpenReview, PMLR, and NeurIPS are enabled")
    return QualityCheck(
        "conference_sources",
        "ICLR/ICML/NeurIPS coverage",
        "partial",
        True,
        f"disabled: {', '.join(missing)}",
        "Enable OpenReview/PMLR/NeurIPS sources for major ML conference coverage.",
    )


def _check_google_scholar(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("google_scholar", {}) or {}
    if not source_config.get("enabled", False):
        return QualityCheck("google_scholar", "Google Scholar", "disabled", False, "google_scholar.enabled is false")
    env_name = str(source_config.get("api_key_env") or "SERPAPI_API_KEY")
    if credential_value(env_name):
        return QualityCheck("google_scholar", "Google Scholar", "full", True, f"{env_name} present; using SerpAPI Google Scholar API")
    if scholarly_runtime_enabled(source_config) and scholarly_available():
        return QualityCheck(
            "google_scholar",
            "Google Scholar",
            "fallback",
            True,
            f"{env_name} missing; supervised scholarly fallback available",
            "Set SERPAPI_API_KEY in the environment or an external secrets TOML file for a more stable Google Scholar path.",
        )
    if source_config.get("scholarly_fallback_enabled", False) and scholarly_available():
        return QualityCheck(
            "google_scholar",
            "Google Scholar",
            "missing",
            False,
            f"{env_name} missing; scholarly is installed but runtime fallback is disabled",
            "Set SERPAPI_API_KEY for unattended runs, or set google_scholar.scholarly_runtime_enabled=true only for supervised local experiments.",
        )
    return QualityCheck(
        "google_scholar",
        "Google Scholar",
        "missing",
        False,
        f"{env_name} missing and scholarly fallback unavailable",
        "Set SERPAPI_API_KEY in the environment or an external secrets TOML file, or install scholarly and enable scholarly_fallback_enabled.",
    )


def _check_semantic_scholar(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("semantic_scholar", {}) or {}
    if not source_config.get("enabled", False):
        return QualityCheck("semantic_scholar", "Semantic Scholar", "disabled", False, "semantic_scholar.enabled is false")
    env_name = str(source_config.get("api_key_env") or "SEMANTIC_SCHOLAR_API_KEY")
    if credential_value(env_name):
        return QualityCheck("semantic_scholar", "Semantic Scholar", "full", True, f"{env_name} present")
    return QualityCheck(
        "semantic_scholar",
        "Semantic Scholar",
        "fallback",
        True,
        f"{env_name} missing; anonymous API may be rate-limited",
        "Set SEMANTIC_SCHOLAR_API_KEY in the environment or an external secrets TOML file for more reliable citation and influence signals.",
    )


def _check_core(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("core", {}) or {}
    if not source_config.get("enabled", False):
        return QualityCheck("core", "CORE", "disabled", False, "core.enabled is false")
    env_name = str(source_config.get("api_key_env") or "CORE_API_KEY")
    if credential_value(env_name):
        return QualityCheck("core", "CORE", "full", True, f"{env_name} present")
    return QualityCheck(
        "core",
        "CORE",
        "missing",
        False,
        f"{env_name} missing; CORE source will be skipped",
        "Set CORE_API_KEY in the environment or an external secrets TOML file to enable CORE works search.",
    )


def _check_ieee(config: AppConfig) -> QualityCheck:
    source_config = config.sources.get("ieee", {}) or {}
    if not source_config.get("enabled", False):
        return QualityCheck("ieee", "IEEE Xplore", "disabled", False, "ieee.enabled is false")
    env_name = str(source_config.get("api_key_env") or "IEEE_XPLORE_API_KEY")
    if credential_value(env_name):
        return QualityCheck("ieee", "IEEE Xplore", "full", True, f"{env_name} present")
    return QualityCheck(
        "ieee",
        "IEEE Xplore",
        "missing",
        False,
        f"{env_name} missing; IEEE source will be skipped",
        "Set IEEE_XPLORE_API_KEY in the environment or an external secrets TOML file to enable IEEE metadata.",
    )


def _check_github(config: AppConfig) -> QualityCheck:
    if not _source_enabled(config, "github"):
        return QualityCheck("github", "GitHub", "disabled", False, "github.enabled is false")
    if credential_value("GITHUB_TOKEN") or credential_value("GH_TOKEN"):
        return QualityCheck("github", "GitHub", "full", True, "GitHub token present")
    return QualityCheck(
        "github",
        "GitHub",
        "fallback",
        True,
        "GITHUB_TOKEN/GH_TOKEN missing; unauthenticated requests may be rate-limited",
        "Set GITHUB_TOKEN or GH_TOKEN in the environment or an external secrets TOML file for more reliable GitHub search and release checks.",
    )


def _check_feishu(config: AppConfig) -> QualityCheck:
    delivery = config.delivery.get("delivery", {}) or {}
    feishu = delivery.get("feishu", {}) or {}
    if not feishu.get("enabled", False):
        return QualityCheck("feishu", "Feishu delivery", "disabled", False, "delivery.feishu.enabled is false")
    app_env = str(feishu.get("app_id_env") or "DAILY_AGENT_FEISHU_APP_ID")
    secret_env = str(feishu.get("app_secret_env") or "DAILY_AGENT_FEISHU_APP_SECRET")
    folder_env = str(feishu.get("folder_token_env") or "DAILY_AGENT_FEISHU_FOLDER_TOKEN")
    doc_env = str(feishu.get("doc_token_env") or "DAILY_AGENT_FEISHU_DOC_TOKEN")
    missing = [env_name for env_name in [app_env, secret_env] if not credential_value(env_name)]
    has_destination = bool(credential_value(folder_env) or credential_value(doc_env))
    if not missing and has_destination:
        return QualityCheck("feishu", "Feishu delivery", "full", True, "Feishu app and destination credentials present")
    if not has_destination:
        missing.append(f"{folder_env} or {doc_env}")
    status = "fallback" if feishu.get("fallback_to_cc_connect", True) else "missing"
    ok = status == "fallback"
    return QualityCheck(
        "feishu",
        "Feishu delivery",
        status,
        ok,
        f"missing {', '.join(missing)}",
        "Set Feishu credentials in the environment, an external secrets TOML file, or cc-connect config; otherwise keep cc-connect/local fallback for local validation.",
    )


def _check_schedule(config: AppConfig) -> QualityCheck:
    schedule = config.delivery.get("schedule", {}) or {}
    production = str(schedule.get("production_time") or "")
    review = str(schedule.get("review_time") or schedule.get("preproduction_time") or "")
    target = str(schedule.get("target_time") or "")
    detail = f"production_time={production or 'missing'}, review_time={review or 'missing'}, target_time={target or 'missing'}"
    if production == "00:10" and review == "05:30" and target == "08:00":
        return QualityCheck("schedule", "Daily workflow schedule", "full", True, detail)
    if production and review and target:
        return QualityCheck("schedule", "Daily workflow schedule", "partial", True, detail)
    return QualityCheck(
        "schedule",
        "Daily workflow schedule",
        "missing",
        False,
        detail,
        "Configure 00:10 collection, 05:30 review checkpoint, and delivery before 08:00 in config/delivery.yaml.",
    )


def _source_enabled(config: AppConfig, key: str) -> bool:
    source_config = config.sources.get(key, {}) or {}
    default = key in {"arxiv", "github"}
    return bool(source_config.get("enabled", default))


def _source_config(sources: dict, source: str) -> dict:
    value = sources.get(source, {}) or {}
    return value if isinstance(value, dict) else {}


def _require_min_int(sources: dict, source: str, key: str, minimum: int, issues: list[str]) -> None:
    value = int(_source_config(sources, source).get(key, 0) or 0)
    if value < minimum:
        issues.append(f"{source}.{key}={value} < {minimum}")


def _require_max_int(sources: dict, source: str, key: str, maximum: int, issues: list[str]) -> None:
    value = int(_source_config(sources, source).get(key, maximum + 1) or 0)
    if value > maximum:
        issues.append(f"{source}.{key}={value} > {maximum}")


def _require_min_number(sources: dict, source: str, key: str, minimum: float, issues: list[str]) -> None:
    value = float(_source_config(sources, source).get(key, 0) or 0)
    if value < minimum:
        issues.append(f"{source}.{key}={value:g} < {minimum:g}")


def _require_max_number(sources: dict, source: str, key: str, maximum: float, issues: list[str]) -> None:
    value = float(_source_config(sources, source).get(key, maximum + 1) or 0)
    if value > maximum:
        issues.append(f"{source}.{key}={value:g} > {maximum:g}")


def _require_bool(sources: dict, source: str, key: str, expected: bool, issues: list[str]) -> None:
    value = bool(_source_config(sources, source).get(key, False))
    if value is not expected:
        issues.append(f"{source}.{key}={str(value).lower()}")


def _require_exact_int(sources: dict, source: str, key: str, expected: int, issues: list[str]) -> None:
    value = int(_source_config(sources, source).get(key, 0) or 0)
    if value != expected:
        issues.append(f"{source}.{key}={value} != {expected}")


def _require_exact_str(sources: dict, source: str, key: str, expected: str, issues: list[str]) -> None:
    value = str(_source_config(sources, source).get(key, "") or "")
    if value != expected:
        issues.append(f"{source}.{key}={value or '<missing>'} != {expected}")


def _require_min_len(sources: dict, source: str, key: str, minimum: int, issues: list[str]) -> None:
    value = _source_config(sources, source).get(key, []) or []
    length = len(value) if isinstance(value, list) else 0
    if length < minimum:
        issues.append(f"{source}.{key}_count={length} < {minimum}")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _package_version(import_name: str) -> str | None:
    if importlib.util.find_spec(import_name) is None:
        return None
    distribution_names = {
        "fitz": ["PyMuPDF", "pymupdf"],
        "pypdf": ["pypdf"],
        "scholarly": ["scholarly"],
    }.get(import_name, [import_name])
    for distribution_name in distribution_names:
        try:
            return importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "installed"
