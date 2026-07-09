from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from daily_agent.config import AppConfig, load_config
from daily_agent.connectors import (
    enrich_github_update_signals,
    fetch_arxiv,
    fetch_citation_discovery,
    fetch_core,
    fetch_crossref,
    fetch_dblp,
    fetch_github,
    fetch_google_scholar,
    fetch_ieee,
    fetch_neurips,
    enrich_open_access_links,
    enrich_unpaywall_links,
    fetch_openalex,
    fetch_openreview,
    fetch_pmlr,
    fetch_semantic_scholar,
    google_scholar_skip_reason,
)
from daily_agent.connectors.paper_text import enrich_paper_texts
from daily_agent.connectors.citation_context import enrich_citation_contexts
from daily_agent.connectors.pdf_cache import cache_selected_pdfs
from daily_agent.delivery.feishu import deliver_weekly_report
from daily_agent.editorial import approve_publication, build_shortlist, draft_report_items, review_draft
from daily_agent.health import evaluate_run_health
from daily_agent.models import ApprovedItem, DeliveryStatus, DigestItem, MaterialRecord, RunStatus, SourceStatus
from daily_agent.rendering import render_daily_html, render_daily_markdown
from daily_agent.scoring import apply_feedback_scores, deduplicate_items, score_items, select_items
from daily_agent.secrets import credential_value
from daily_agent.storage import (
    cleanup_retention,
    load_health_report,
    load_history,
    load_material_library,
    mark_materials_published,
    select_library_candidates,
    upsert_materials,
    write_editorial_artifacts,
    write_bibtex_export,
    write_csv_export,
    write_daily_html_report,
    write_daily_report,
    write_endnote_xml_export,
    write_published_index,
    write_ris_export,
    write_health_report,
    write_run_log,
    write_selected,
    write_weekly_html_report,
    write_weekly_report,
)


@dataclass
class PipelineResult:
    run_date: date
    items: list[ApprovedItem]
    daily_markdown: str
    daily_report_path: Path
    daily_html_path: Path
    weekly_report_path: Path
    weekly_html_path: Path
    selected_path: Path
    bibtex_path: Path
    ris_path: Path
    csv_path: Path
    endnote_xml_path: Path
    status: RunStatus
    health: dict[str, Any]
    health_path: Path


def run_pipeline(
    root: str | Path | None = None,
    run_date: date | None = None,
    dry_run: bool = True,
    use_llm: bool = True,
    delivery_mode: str = "local",
) -> PipelineResult:
    config = load_config(root)
    today = run_date or date.today()
    target_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    status = RunStatus()

    history = {} if dry_run else load_history(config, today)
    raw_items: list[DigestItem] = []
    selected_items: list[DigestItem] = []
    shortlist = []
    expanded = False
    for index, (arxiv_window, github_window) in enumerate(_fallback_window_steps(config)):
        if index > 0:
            expanded = True
        window_items = _fetch_windowed_sources(config, target_dt, arxiv_window, github_window, status)
        raw_items.extend(window_items)
        selected_items = _prepare_selected_items(raw_items, config, history, target_dt)
        preview_library = dict(load_material_library(config))
        for key, record in upsert_materials_preview(preview_library, selected_items, today).items():
            preview_library[key] = record
        shortlist = _build_shortlist_from_library(config, preview_library, today)
        if len(shortlist) >= _shortlist_target(config):
            break
    if expanded and not status.fallback:
        status.fallback = "内容不足，已扩展检索窗口"

    if selected_items:
        upsert_materials(config, selected_items, today)
    library = load_material_library(config)
    shortlist = _build_shortlist_from_library(config, library, today)
    shortlist = enrich_open_access_links(shortlist, config)
    shortlist = enrich_unpaywall_links(shortlist, config)
    shortlist = enrich_paper_texts(shortlist, config)
    shortlist = enrich_citation_contexts(shortlist, config)
    drafts = draft_report_items(config, shortlist, use_llm=use_llm)
    reviews = review_draft(config, drafts, use_llm=use_llm)
    approved = approve_publication(config, shortlist, drafts, reviews)
    approved = cache_selected_pdfs(approved, config, today)

    if not approved:
        status.fallback = "编辑部未批准任何候选内容"

    insight_config = config.sources.get("insights", {}) or {}
    daily_markdown = render_daily_markdown(approved, today, status, insight_config=insight_config)
    daily_html = render_daily_html(approved, today, status, insight_config=insight_config)
    daily_report_path = write_daily_report(config, today, daily_markdown)
    daily_html_path = write_daily_html_report(config, today, daily_html)
    weekly_report_path = write_weekly_report(config, today, daily_markdown)
    weekly_html_path = write_weekly_html_report(config, today, daily_html)
    selected_path = write_selected(config, approved, today, dry_run=dry_run)
    bibtex_path = write_bibtex_export(config, approved, today, dry_run=dry_run)
    ris_path = write_ris_export(config, approved, today, dry_run=dry_run)
    csv_path = write_csv_export(config, approved, today, dry_run=dry_run)
    endnote_xml_path = write_endnote_xml_export(config, approved, today, dry_run=dry_run)
    editorial_path = write_editorial_artifacts(config, today, shortlist, drafts, reviews, approved)
    if not dry_run:
        mark_materials_published(config, [item.material for item in approved], today)
        write_published_index(config, approved, today)
    cleanup_retention(config, today)

    if dry_run:
        status.delivery = DeliveryStatus(requested_mode="dry-run", final_mode="local", ok=True)
    else:
        try:
            status.delivery = deliver_weekly_report(
                weekly_report_path,
                delivery_mode,
                config=config,
                run_date=today,
                daily_markdown=daily_markdown,
            )
        except Exception as exc:
            status.delivery = DeliveryStatus(requested_mode=delivery_mode, final_mode="local", ok=False, error=str(exc))
            status.errors.append(f"Delivery: {exc}")
    if status.delivery and not status.delivery.ok and status.delivery.error:
        _add_error_once(status, f"Delivery: {status.delivery.error}")
    _run_post_delivery_checks(
        status,
        approved,
        weekly_report_path,
        weekly_html_path,
        selected_path,
        delivery_mode,
        dry_run,
        config=config,
        daily_report_path=daily_report_path,
        daily_html_path=daily_html_path,
    )

    write_run_log(config, today, status, dry_run=dry_run)
    previous_health = load_health_report(config)
    health = evaluate_run_health(config, today, dry_run, status, approved, shortlist, drafts, reviews, weekly_report_path, weekly_html_path, selected_path, editorial_path, previous_health, use_llm=use_llm)
    health_path = write_health_report(config, health)

    return PipelineResult(
        run_date=today,
        items=approved,
        daily_markdown=daily_markdown,
        daily_report_path=daily_report_path,
        daily_html_path=daily_html_path,
        weekly_report_path=weekly_report_path,
        weekly_html_path=weekly_html_path,
        selected_path=selected_path,
        bibtex_path=bibtex_path,
        ris_path=ris_path,
        csv_path=csv_path,
        endnote_xml_path=endnote_xml_path,
        status=status,
        health=health,
        health_path=health_path,
    )


def _fallback_window_steps(config: AppConfig) -> list[tuple[int, int]]:
    arxiv_config = config.sources.get("arxiv", {}) or {}
    github_config = config.sources.get("github", {}) or {}
    recent_days = int(arxiv_config.get("recent_days", 7))
    arxiv_windows = _unique_sorted([recent_days, *[int(value) for value in arxiv_config.get("fallback_windows_days", [])]])
    normal_days = int(github_config.get("normal_active_days", 30))
    high_days = int(github_config.get("high_relevance_active_days", normal_days))
    steps = []
    for arxiv_window in arxiv_windows:
        github_window = max(normal_days, min(max(arxiv_window, normal_days), max(high_days, normal_days)))
        steps.append((arxiv_window, github_window))
    return steps or [(recent_days, normal_days)]


def _unique_sorted(values: list[int]) -> list[int]:
    return sorted({value for value in values if value > 0})


def _fetch_windowed_sources(config: AppConfig, target_dt: datetime, arxiv_window: int, github_window: int, status: RunStatus) -> list[DigestItem]:
    raw_items: list[DigestItem] = []
    raw_items.extend(_fetch_source(f"arXiv/{arxiv_window}d", lambda: fetch_arxiv(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"GitHub/{github_window}d", lambda: fetch_github(config, target_dt, window_days=github_window), status))
    raw_items.extend(_fetch_source(f"OpenAlex/{arxiv_window}d", lambda: fetch_openalex(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"Semantic Scholar/{arxiv_window}d", lambda: fetch_semantic_scholar(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"Citation Discovery/{arxiv_window}d", lambda: fetch_citation_discovery(config, target_dt, window_days=arxiv_window, library=load_material_library(config)), status))
    raw_items.extend(_fetch_source(f"Google Scholar/{arxiv_window}d", lambda: fetch_google_scholar(config, target_dt, window_days=arxiv_window), status, skip_reason=google_scholar_skip_reason(config)))
    raw_items.extend(_fetch_source(f"Crossref/{arxiv_window}d", lambda: fetch_crossref(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"CORE/{arxiv_window}d", lambda: fetch_core(config, target_dt, window_days=arxiv_window), status, skip_reason=_core_skip_reason(config)))
    raw_items.extend(_fetch_source(f"DBLP/{arxiv_window}d", lambda: fetch_dblp(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"IEEE/{arxiv_window}d", lambda: fetch_ieee(config, target_dt, window_days=arxiv_window), status, skip_reason=_ieee_skip_reason(config)))
    raw_items.extend(_fetch_source(f"OpenReview/{arxiv_window}d", lambda: fetch_openreview(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"PMLR/{arxiv_window}d", lambda: fetch_pmlr(config, target_dt, window_days=arxiv_window), status))
    raw_items.extend(_fetch_source(f"NeurIPS/{arxiv_window}d", lambda: fetch_neurips(config, target_dt, window_days=arxiv_window), status))
    return raw_items


def _prepare_selected_items(raw_items: list[DigestItem], config: AppConfig, history, target_dt: datetime) -> list[DigestItem]:
    items = deduplicate_items(raw_items)
    if history:
        max_update_signal_repos = int(config.sources.get("github", {}).get("update_signal_max_repos", 10))
        items = enrich_github_update_signals(items, history, max_repos=max_update_signal_repos)
    items = score_items(items, config, history, target_dt)
    items = apply_feedback_scores(items, config)
    items = score_items(items, config, history, target_dt)
    selected_items = select_items(items, config)
    return _ensure_historical_supplement(selected_items, items, config)


def upsert_materials_preview(library: dict[str, MaterialRecord], items: list[DigestItem], run_date: date) -> dict[str, MaterialRecord]:
    stamp = datetime(run_date.year, run_date.month, run_date.day, tzinfo=timezone.utc).isoformat()
    for item in items:
        key = item.canonical_key()
        incoming = MaterialRecord.from_item(item, seen_at=stamp)
        existing = library.get(key)
        if existing:
            incoming.first_seen_at = existing.first_seen_at
            incoming.published_dates = existing.published_dates
            incoming.quality_status = existing.quality_status if existing.quality_status in {"published", "archived", "rejected"} else incoming.quality_status
            incoming.readme_excerpt = existing.readme_excerpt or incoming.readme_excerpt
            incoming.paper_text_excerpt = existing.paper_text_excerpt or incoming.paper_text_excerpt
            incoming.paper_text_status = incoming.paper_text_status or existing.paper_text_status
            incoming.detail = {**existing.detail, **incoming.detail}
            incoming.source_aliases = {**existing.source_aliases, **incoming.source_aliases}
            incoming.evidence = {"sources": {**((existing.evidence or {}).get("sources", {}) or {}), **((incoming.evidence or {}).get("sources", {}) or {})}}
        library[key] = incoming
    return library


def _build_shortlist_from_library(config: AppConfig, library: dict[str, MaterialRecord], today: date) -> list[MaterialRecord]:
    candidates = select_library_candidates(config, library, today)
    return build_shortlist(config, {record.key: record for record in candidates}, today)


def _shortlist_target(config: AppConfig) -> int:
    max_items = int(config.quota.get("max_items", 10))
    paper_target = int(config.quota.get("paper_target", 7))
    github_target = int(config.quota.get("github_target", 3))
    multiplier = max(1, int(config.quota.get("paper_review_multiplier", 2)))
    paper_review_target = max(paper_target, int(config.quota.get("paper_review_target", paper_target * multiplier)))
    return max(max_items, paper_review_target + github_target)


def _run_post_delivery_checks(
    status: RunStatus,
    approved: list[ApprovedItem],
    weekly_report_path: Path,
    weekly_html_path: Path,
    selected_path: Path,
    delivery_mode: str,
    dry_run: bool,
    config: AppConfig | None = None,
    daily_report_path: Path | None = None,
    daily_html_path: Path | None = None,
) -> None:
    if not approved:
        _add_error_once(status, "No approved items")
    if config is not None:
        min_papers = int(config.quota.get("paper_target", 7))
        approved_papers = sum(1 for item in approved if getattr(item, "item_type", None) == "paper")
        if approved_papers < min_papers:
            _add_error_once(status, f"Approved papers below target: {approved_papers}/{min_papers}")
    if not weekly_report_path.exists():
        _add_error_once(status, f"Weekly report missing: {weekly_report_path}")
    if not weekly_html_path.exists():
        _add_error_once(status, f"Weekly HTML report missing: {weekly_html_path}")
    if daily_report_path is not None and not daily_report_path.exists():
        _add_error_once(status, f"Daily report missing: {daily_report_path}")
    if daily_html_path is not None and not daily_html_path.exists():
        _add_error_once(status, f"Daily HTML report missing: {daily_html_path}")
    if not selected_path.exists():
        _add_error_once(status, f"Selected JSON missing: {selected_path}")
    if dry_run or delivery_mode != "feishu" or not status.delivery:
        return
    if status.delivery.fallback_used or status.delivery.final_mode != "feishu":
        detail = status.delivery.error or "unknown error"
        _add_error_once(status, f"Feishu delivery used fallback: {detail}")
    elif status.delivery.ok and not status.delivery.document_url:
        _add_error_once(status, "Feishu delivery missing document URL")


def _add_error_once(status: RunStatus, message: str) -> None:
    if message not in status.errors:
        status.errors.append(message)


def _fetch_source(name: str, fetcher: Callable[[], list[DigestItem]], status: RunStatus, skip_reason: str | None = None) -> list[DigestItem]:
    if skip_reason:
        status.sources.append(SourceStatus(name=name, ok=True, item_count=0, error=skip_reason, skipped=True))
        return []
    last_error: Exception | None = None
    retries = 1
    for attempt in range(retries + 1):
        try:
            items = fetcher()
            status.sources.append(SourceStatus(name=name, ok=True, item_count=len(items), retries=attempt))
            return items
        except Exception as exc:
            last_error = exc
    message = str(last_error) if last_error else "未知错误"
    status.sources.append(SourceStatus(name=name, ok=False, retries=retries, error=message))
    status.errors.append(f"{name}: {message}")
    normalized_name = name.lower()
    if normalized_name.startswith("arxiv"):
        status.fallback = "arXiv 失败，使用其他数据源与历史补充"
    elif normalized_name.startswith("github"):
        status.fallback = "GitHub 失败，使用论文数据源与历史补充"
    return []


def _ieee_skip_reason(config: AppConfig) -> str | None:
    source_config = config.sources.get("ieee", {}) or {}
    if not source_config.get("enabled", False):
        return "source disabled"
    env_name = str(source_config.get("api_key_env") or "IEEE_XPLORE_API_KEY")
    if not credential_value(env_name):
        return f"missing {env_name}"
    return None


def _core_skip_reason(config: AppConfig) -> str | None:
    source_config = config.sources.get("core", {}) or {}
    if not source_config.get("enabled", False):
        return "source disabled"
    env_name = str(source_config.get("api_key_env") or "CORE_API_KEY")
    if not credential_value(env_name):
        return f"missing {env_name}"
    return None


def _ensure_historical_supplement(
    selected: list[DigestItem],
    scored_items: list[DigestItem],
    config: AppConfig,
) -> list[DigestItem]:
    max_items = int(config.quota.get("max_items", 10))
    if len(selected) >= max_items:
        return selected
    min_score = float(config.sources.get("selection", {}).get("historical_min_score", 25))
    selected_keys = {item.canonical_key() for item in selected}
    supplements = [
        item
        for item in scored_items
        if item.canonical_key() not in selected_keys
        and item.is_historical_supplement
        and item.score >= min_score
        and item.score > -50
    ]
    for item in supplements:
        if len(selected) >= max_items:
            break
        selected.append(item)
        selected_keys.add(item.canonical_key())
    return sorted(selected, key=lambda item: item.score, reverse=True)
