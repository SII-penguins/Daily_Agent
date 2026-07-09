from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


FULL_SOURCES_PROFILE: dict[str, Any] = {
    "arxiv": {
        "enabled": True,
        "recent_days": 7,
        "fallback_windows_days": [30, 90, 180, 365],
        "max_results_per_query": 100,
        "max_queries_per_domain": 8,
        "request_delay_seconds": 3,
    },
    "github": {
        "enabled": True,
        "search_enabled": True,
        "trending_enabled": True,
        "normal_active_days": 30,
        "high_relevance_active_days": 90,
        "max_results_per_query": 50,
        "max_queries_per_domain": 20,
        "update_signal_max_repos": 50,
        "min_stars": 0,
        "timeout_seconds": 30,
        "trending_languages": ["python", "typescript", "jupyter-notebook", "rust"],
    },
    "llm_writer": {"batch_size": 2, "timeout_seconds": 90, "run_budget_seconds": 180},
    "openalex": {
        "enabled": True,
        "recent_days": 7,
        "max_results_per_query": 100,
        "max_queries_per_domain": 8,
        "timeout_seconds": 60,
        "allowed_publication_types": ["article", "review", "preprint", "proceedings-article"],
    },
    "semantic_scholar": {
        "enabled": True,
        "recent_days": 7,
        "max_results_per_query": 100,
        "max_queries_per_domain": 8,
        "timeout_seconds": 60,
        "api_key_env": "SEMANTIC_SCHOLAR_API_KEY",
        "allowed_publication_types": ["JournalArticle", "Conference", "Review", "Preprint"],
    },
    "google_scholar": {
        "enabled": True,
        "recent_days": 30,
        "provider": "serpapi",
        "api_key_env": "SERPAPI_API_KEY",
        "scholarly_fallback_enabled": True,
        "scholarly_runtime_enabled": False,
        "max_results_per_query": 40,
        "max_queries_per_domain": 8,
        "scholarly_max_results_per_query": 20,
        "sort_by_date": True,
        "scisbd": 2,
        "source_check_timeout_seconds": 15,
        "scholarly_runtime_timeout_seconds": 60,
        "timeout_seconds": 60,
        "cite_enrichment_enabled": True,
        "cite_enrichment_max_results_per_run": 50,
        "cite_cache_enabled": True,
        "cite_cache_path": "data/cache/google_scholar_cites.json",
        "cite_cache_max_entries": 50_000,
    },
    "crossref": {
        "enabled": True,
        "recent_days": 7,
        "max_results_per_query": 100,
        "max_queries_per_domain": 8,
        "timeout_seconds": 60,
        "allowed_publication_types": ["journal-article", "proceedings-article", "posted-content"],
    },
    "core": {"enabled": True, "recent_days": 30, "max_results_per_query": 100, "max_queries_per_domain": 8, "timeout_seconds": 60, "api_key_env": "CORE_API_KEY"},
    "dblp": {"enabled": True, "recent_years": 2, "max_results_per_query": 100, "max_queries_per_domain": 8, "timeout_seconds": 60},
    "ieee": {"enabled": True, "recent_days": 7, "max_results_per_query": 100, "max_queries_per_domain": 8, "timeout_seconds": 60, "api_key_env": "IEEE_XPLORE_API_KEY"},
    "openreview": {
        "enabled": True,
        "recent_days": 30,
        "max_results_per_venue": 100,
        "timeout_seconds": 60,
        "venues": [
            {"name": "ICLR 2026", "invitation": "ICLR.cc/2026/Conference/-/Submission"},
            {"name": "ICLR 2025", "invitation": "ICLR.cc/2025/Conference/-/Submission"},
        ],
    },
    "pmlr": {
        "enabled": True,
        "recent_days": 365,
        "max_results_per_volume": 100,
        "timeout_seconds": 60,
        "volumes": [
            {"venue": "ICML 2025", "url": "https://proceedings.mlr.press/v267/"},
            {"venue": "ICML 2024", "url": "https://proceedings.mlr.press/v235/"},
        ],
    },
    "neurips": {"enabled": True, "recent_days": 365, "max_results_per_year": 100, "max_detail_pages_per_year": 100, "timeout_seconds": 60, "years": [2026, 2025, 2024]},
    "paper_text": {
        "enabled": True,
        "max_papers_per_run": 50,
        "max_pdf_bytes": 30_000_000,
        "html_fallback_enabled": True,
        "max_html_bytes": 20_000_000,
        "max_excerpt_chars": 100_000,
        "max_raw_text_chars": 300_000,
        "section_notes_enabled": True,
        "max_section_note_chars": 2400,
        "timeout_seconds": 15,
        "max_urls_per_paper": 4,
        "run_budget_seconds": 180,
    },
    "oa_resolver": {
        "enabled": True,
        "max_papers_per_run": 50,
        "timeout_seconds": 30,
        "run_budget_seconds": 120,
    },
    "unpaywall": {
        "enabled": True,
        "email_env": "UNPAYWALL_EMAIL",
        "max_papers_per_run": 50,
        "timeout_seconds": 15,
        "run_budget_seconds": 120,
        "cache_enabled": True,
        "cache_path": "data/cache/unpaywall.json",
        "cache_max_entries": 50_000,
    },
    "pdf_cache": {
        "enabled": True,
        "max_papers_per_run": 10,
        "max_pdf_bytes": 30_000_000,
        "max_urls_per_paper": 4,
        "timeout_seconds": 15,
        "run_budget_seconds": 180,
        "output_dir": "data/pdfs",
    },
    "citation_context": {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_citing_papers": 8,
        "max_referenced_papers": 8,
        "timeout_seconds": 60,
    },
    "citation_discovery": {
        "enabled": True,
        "recent_days": 30,
        "max_seed_papers": 20,
        "max_results_per_seed": 5,
        "timeout_seconds": 60,
    },
    "query_expansion": {"enabled": True, "max_expand_terms_per_domain": 16},
    "insights": {"enabled": True, "include_in_reports": True, "max_insights": 5, "min_items": 2, "research_gap_enabled": True},
    "exports": {"bibtex_enabled": True, "ris_enabled": True, "csv_enabled": True, "endnote_xml_enabled": True},
    "selection": {
        "repeat_suppression_days": 30,
        "historical_min_score": 15,
        "top_candidates_for_llm": 50,
        "topic_relevance_gate_enabled": True,
        "min_topic_relevance_score": 6,
        "off_topic_score_penalty": -120,
        "debug_include_rejected": False,
    },
}

FULL_INTERESTS_PROFILE: dict[str, Any] = {
    "quota": {
        "max_items": 10,
        "paper_target": 7,
        "paper_review_multiplier": 4,
        "github_target": 3,
    }
}

FULL_DELIVERY_PROFILE: dict[str, Any] = {
    "report": {"timezone": "Asia/Shanghai"},
    "delivery": {
        "cc_connect": {"enabled": True, "send_file": True},
        "feishu": {"enabled": True, "prefer_cloud_doc": True, "fallback_to_cc_connect": True, "request_timeout_seconds": 30},
    },
    "schedule": {"production_time": "02:00", "review_time": "07:20", "target_time": "08:00", "preproduction_time": "07:20"},
}

FULL_FEEDBACK_PROFILE: dict[str, Any] = {
    "liked": {"tags": [], "keywords": []},
    "disliked": {"tags": [], "keywords": []},
    "events": {
        "enabled": True,
        "lookback_days": 90,
        "half_life_days": 30,
        "exact_item_like_weight": 8,
        "exact_item_dislike_weight": -10,
        "tag_like_weight": 2,
        "tag_dislike_weight": -3,
        "keyword_like_weight": 3,
        "keyword_dislike_weight": -4,
        "max_feedback_score": 12,
        "min_feedback_score": -15,
    },
    "feishu_comments": {"enabled": True, "file_type": "docx", "page_size": 50, "include_replies": True},
}


@dataclass(frozen=True)
class FullProfileResult:
    changed: bool
    written: bool
    lines: list[str]


def enforce_full_profile(root: str | Path, write: bool = False) -> FullProfileResult:
    if yaml is None:
        raise RuntimeError("PyYAML is required to enforce the full-quality profile")
    root_path = Path(root)
    changes: list[str] = []
    files = [
        (root_path / "config" / "sources.yaml", "config/sources.yaml", FULL_SOURCES_PROFILE),
        (root_path / "config" / "interests.yaml", "config/interests.yaml", FULL_INTERESTS_PROFILE),
        (root_path / "config" / "delivery.yaml", "config/delivery.yaml", FULL_DELIVERY_PROFILE),
        (root_path / "config" / "feedback.yaml", "config/feedback.yaml", FULL_FEEDBACK_PROFILE),
    ]
    updated_payloads: list[tuple[Path, dict[str, Any]]] = []
    for path, label, profile in files:
        payload = _load_yaml(path)
        before_count = len(changes)
        _apply_profile(payload, profile, label, [], changes)
        if len(changes) > before_count:
            updated_payloads.append((path, payload))
    if write:
        for path, payload in updated_payloads:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return FullProfileResult(changed=bool(changes), written=write and bool(changes), lines=changes)


def render_full_profile_result(result: FullProfileResult) -> str:
    if not result.changed:
        return "Full-profile enforcement: no changes needed"
    status = "applied" if result.written else "changes pending"
    lines = [f"Full-profile enforcement: {status}"]
    lines.extend(f"- {line}" for line in result.lines)
    if not result.written:
        lines.append("Run again with --write to apply these config changes.")
    return "\n".join(lines)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded if isinstance(loaded, dict) else {}


def _apply_profile(payload: dict[str, Any], profile: dict[str, Any], label: str, path: list[str], changes: list[str]) -> None:
    for key, desired in profile.items():
        current_path = [*path, key]
        if isinstance(desired, dict):
            current = payload.get(key)
            if not isinstance(current, dict):
                current_missing = key not in payload
                payload[key] = {}
                changes.append(f"{label}: {'.'.join(current_path)} {_format_value(current, missing=current_missing)} -> {{}}")
            _apply_profile(payload[key], desired, label, current_path, changes)
            continue
        current_missing = key not in payload
        current = payload.get(key)
        if current != desired:
            changes.append(f"{label}: {'.'.join(current_path)} {_format_value(current, missing=current_missing)} -> {_format_value(desired)}")
            payload[key] = desired


def _format_value(value: Any, missing: bool = False) -> str:
    if missing:
        return "<missing>"
    return str(value)
