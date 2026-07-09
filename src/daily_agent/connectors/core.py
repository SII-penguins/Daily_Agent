from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.secrets import credential_value

CORE_WORKS_SEARCH_URL = "https://api.core.ac.uk/v3/search/works"


def fetch_core(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("core", {}) or {}
    if not source_config.get("enabled", False):
        return []
    env_name = str(source_config.get("api_key_env") or "CORE_API_KEY")
    api_key = credential_value(env_name)
    if not api_key:
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(
                        CORE_WORKS_SEARCH_URL,
                        params=_params(query, target, days, max_results),
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                for work in response.json().get("results", []) or []:
                    item = _work_to_item(work, domain)
                    if item:
                        items.append(item)
    return _dedupe(items)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _params(query: str, target: datetime, days: int, max_results: int) -> dict[str, str | int]:
    start = target - timedelta(days=days)
    year_filter = str(target.year) if start.year == target.year else f"{start.year}-{target.year}"
    return {"q": query, "limit": max_results, "yearFilter": year_filter}


def _work_to_item(work: dict[str, Any], domain: DomainConfig) -> DigestItem | None:
    title = str(work.get("title") or "").strip()
    if not title:
        return None
    core_id = str(work.get("id") or work.get("coreId") or title)
    doi = _normalize_doi(work.get("doi"))
    core_url = _first_url(work.get("fullTextLink")) or _first_url(work.get("links")) or f"https://core.ac.uk/works/{core_id}"
    pdf_url = _first_url(work.get("downloadUrl")) or _first_url(work.get("sourceFulltextUrls"))
    published_at = _published_at(work)
    topics = _topics(work.get("topics"))
    return DigestItem(
        id=core_id,
        source="core",
        item_type="paper",
        title=title,
        url=core_url,
        pdf_url=pdf_url,
        authors=_authors(work.get("authors")),
        abstract=_clean_text(work.get("abstract")),
        published_at=published_at,
        updated_at=published_at,
        source_tags=["core", domain.name, *topics],
        categories=topics,
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "core_id": core_id,
            "core_url": core_url,
            "venue": work.get("publisher") or work.get("source"),
            "publisher": work.get("publisher"),
            "citation_count": work.get("citationCount"),
            "topics": topics,
            "source_fulltext_urls": _urls(work.get("sourceFulltextUrls")),
        },
    )


def _authors(value: Any) -> list[str]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    authors = []
    for author in values:
        if isinstance(author, dict):
            name = author.get("name") or author.get("fullName") or author.get("full_name")
        else:
            name = author
        if name:
            authors.append(str(name).strip())
    return [author for author in authors if author]


def _topics(value: Any) -> list[str]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    topics = []
    for item in values:
        if isinstance(item, dict):
            topic = item.get("name") or item.get("label") or item.get("title")
        else:
            topic = item
        if topic:
            topics.append(str(topic).strip())
    return [topic for topic in topics if topic]


def _published_at(work: dict[str, Any]) -> str | None:
    value = work.get("publishedDate") or work.get("published_date") or work.get("publicationDate")
    if value:
        text = str(value)
        return text[:10] if len(text) >= 10 else text
    year = work.get("yearPublished") or work.get("year")
    try:
        return f"{int(year):04d}-01-01"
    except (TypeError, ValueError):
        return None


def _first_url(value: Any) -> str | None:
    urls = _urls(value)
    return urls[0] if urls else None


def _urls(value: Any) -> list[str]:
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    urls = []
    for item in values:
        if isinstance(item, dict):
            url = item.get("url") or item.get("link")
        else:
            url = item
        if url:
            urls.append(str(url))
    return urls


def _clean_text(value: Any) -> str | None:
    if not value:
        return None
    return " ".join(str(value).split())


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    doi = str(value).strip()
    doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    return doi.lower()


def _dedupe(items: list[DigestItem]) -> list[DigestItem]:
    seen: set[str] = set()
    result: list[DigestItem] = []
    for item in items:
        key = item.canonical_key()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
