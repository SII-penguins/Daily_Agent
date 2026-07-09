from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.secrets import credential_value

IEEE_SEARCH_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"


def fetch_ieee(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("ieee", {}) or {}
    if not source_config.get("enabled", False):
        return []
    env_name = str(source_config.get("api_key_env") or "IEEE_XPLORE_API_KEY")
    api_key = credential_value(env_name)
    if not api_key:
        return []
    target = target_date or datetime.now(timezone.utc)
    max_results = int(source_config.get("max_results_per_query", 10))
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
                    response = client.get(
                        IEEE_SEARCH_URL,
                        params={
                            "apikey": api_key,
                            "querytext": query,
                            "max_records": max_results,
                            "sort_field": "publication_year",
                            "sort_order": "desc",
                            "start_record": 1,
                        },
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                for article in response.json().get("articles", []) or []:
                    item = _article_to_item(article, domain)
                    if not item.published_at or item.published_at[:4] >= str(target.year - 1):
                        items.append(item)
    return items


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _article_to_item(article: dict[str, Any], domain: DomainConfig) -> DigestItem:
    article_number = str(article.get("article_number") or article.get("articleNumber") or "")
    doi = _normalize_doi(article.get("doi"))
    url = article.get("html_url") or article.get("abstract_url") or (f"https://ieeexplore.ieee.org/document/{article_number}" if article_number else "")
    venue = article.get("publication_title")
    year = str(article.get("publication_year") or "") or None
    return DigestItem(
        id=article_number or doi or str(article.get("title") or ""),
        source="ieee",
        item_type="paper",
        title=str(article.get("title") or "untitled"),
        url=url,
        pdf_url=article.get("pdf_url"),
        authors=[str(author.get("full_name")) for author in (article.get("authors") or {}).get("authors", []) or [] if author.get("full_name")],
        abstract=article.get("abstract"),
        published_at=year,
        updated_at=year,
        source_tags=["ieee", domain.name, venue or ""],
        categories=[venue] if venue else [],
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "ieee_article_number": article_number,
            "ieee_url": url,
            "venue": venue,
            "publisher": "IEEE",
        },
    )


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    return str(value).removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
