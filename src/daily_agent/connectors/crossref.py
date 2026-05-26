from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.models import DigestItem

CROSSREF_WORKS_URL = "https://api.crossref.org/works"


def fetch_crossref(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("crossref", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for domain in config.domains:
            for query in _queries(domain):
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(CROSSREF_WORKS_URL, params=_params(query, target, days, max_results))
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                for work in response.json().get("message", {}).get("items", []) or []:
                    items.append(_work_to_item(work, domain))
    return items


def _queries(domain: DomainConfig) -> list[str]:
    return domain.include_keywords or domain.github_queries or [domain.name]


def _params(query: str, target: datetime, days: int, max_results: int) -> dict[str, str | int]:
    start = (target - timedelta(days=days)).date().isoformat()
    end = (target + timedelta(days=1)).date().isoformat()
    return {
        "query.bibliographic": query,
        "filter": f"from-pub-date:{start},until-pub-date:{end}",
        "sort": "published",
        "order": "desc",
        "rows": max_results,
    }


def _work_to_item(work: dict[str, Any], domain: DomainConfig) -> DigestItem:
    doi = _normalize_doi(work.get("DOI"))
    title = _first(work.get("title")) or "untitled"
    venue = _first(work.get("container-title"))
    published_at = _date_parts(work.get("published-online") or work.get("published-print") or work.get("created"))
    return DigestItem(
        id=doi or str(work.get("URL") or title),
        source="crossref",
        item_type="paper",
        title=title,
        url=work.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
        authors=[_author_name(author) for author in work.get("author", []) or [] if _author_name(author)],
        abstract=_clean_abstract(work.get("abstract")),
        published_at=published_at,
        updated_at=published_at,
        source_tags=["crossref", domain.name, venue or ""],
        categories=[venue] if venue else [],
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "venue": venue,
            "publisher": work.get("publisher"),
            "cited_by_count": work.get("is-referenced-by-count"),
        },
    )


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value) if value else None


def _date_parts(value: Any) -> str | None:
    parts = (value or {}).get("date-parts") or []
    if not parts or not parts[0]:
        return None
    year, month, day = [*parts[0], 1, 1][:3]
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _author_name(author: dict[str, Any]) -> str:
    return " ".join(str(author.get(key) or "").strip() for key in ["given", "family"]).strip()


def _clean_abstract(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    return str(value).removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
