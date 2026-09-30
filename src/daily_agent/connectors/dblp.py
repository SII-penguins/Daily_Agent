from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem

DBLP_PUBLICATION_SEARCH_URL = "https://dblp.org/search/publ/api"


def fetch_dblp(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("dblp", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    min_year = _min_year(target, source_config, window_days)
    max_results = int(source_config.get("max_results_per_query", 20))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(DBLP_PUBLICATION_SEARCH_URL, params={"q": query, "format": "json", "h": max_results, "f": 0})
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    # DBLP occasionally returns an HTML gateway page with 200;
                    # skip that query and keep the remaining domains usable.
                    continue
                for hit in _hits(payload):
                    item = _hit_to_item(hit, domain)
                    if item and _year(item) >= min_year:
                        items.append(item)
    return _dedupe(items)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _min_year(target: datetime, source_config: dict[str, Any], window_days: int | None) -> int:
    if window_days is not None:
        return (target - timedelta(days=int(window_days))).year
    recent_years = int(source_config.get("recent_years", 2))
    return target.year - max(recent_years, 1) + 1


def _hits(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    hits = ((payload.get("result") or {}).get("hits") or {}).get("hit") or []
    if isinstance(hits, dict):
        return [hits]
    return hits if isinstance(hits, list) else []


def _hit_to_item(hit: dict[str, Any], domain: DomainConfig) -> DigestItem | None:
    info = hit.get("info") or {}
    title = _clean_title(info.get("title"))
    if not title:
        return None
    dblp_key = str(info.get("key") or hit.get("@id") or title)
    dblp_url = str(info.get("url") or f"https://dblp.org/rec/{dblp_key}")
    doi = _normalize_doi(info.get("doi") or info.get("ee"))
    venue = str(info.get("venue") or "")
    year = _safe_year(info.get("year"))
    published_at = f"{year:04d}-01-01" if year else None
    return DigestItem(
        id=dblp_key,
        source="dblp",
        item_type="paper",
        title=title,
        url=dblp_url,
        authors=_authors(info.get("authors")),
        published_at=published_at,
        updated_at=published_at,
        source_tags=["dblp", domain.name, venue],
        categories=[value for value in [venue, str(info.get("type") or "")] if value],
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "dblp_key": dblp_key,
            "dblp_url": dblp_url,
            "venue": venue,
            "publication_type": info.get("type"),
            "year": year,
            "ee": info.get("ee"),
        },
    )


def _authors(value: Any) -> list[str]:
    raw = (value or {}).get("author") if isinstance(value, dict) else value
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    authors = []
    for item in values:
        if isinstance(item, dict):
            name = item.get("text") or item.get("@pid")
        else:
            name = item
        if name:
            authors.append(str(name))
    return authors


def _clean_title(value: Any) -> str:
    return str(value or "").strip().rstrip(".").strip()


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if "doi.org/" in text:
        text = text.rsplit("doi.org/", 1)[-1]
    if text.lower().startswith("http"):
        return None
    return text.lower()


def _safe_year(value: Any) -> int | None:
    try:
        year = int(str(value))
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _year(item: DigestItem) -> int:
    try:
        return int((item.published_at or "0000")[:4])
    except ValueError:
        return 0


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
