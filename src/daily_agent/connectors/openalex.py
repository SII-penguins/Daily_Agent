from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import time

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.publication_types import allowed_publication_types, publication_type_allowed
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem

OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def fetch_openalex(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("openalex", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    allowed_types = allowed_publication_types(source_config)
    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    headers = {"User-Agent": "Daily-Agent/0.1"}
    mailto = str(source_config.get("mailto") or "").strip()
    if mailto:
        headers["User-Agent"] = f"Daily-Agent/0.1 (mailto:{mailto})"
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(OPENALEX_WORKS_URL, params=_params(query, target, days, max_results))
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                for work in response.json().get("results", []) or []:
                    if not publication_type_allowed(work.get("type"), allowed_types):
                        continue
                    items.append(_work_to_item(work, domain))
    return _dedupe(items)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _params(query: str, target: datetime, days: int, max_results: int) -> dict[str, str | int]:
    start = (target - timedelta(days=days)).date().isoformat()
    end = (target + timedelta(days=1)).date().isoformat()
    return {
        "search": query,
        "filter": f"from_publication_date:{start},to_publication_date:{end}",
        "sort": "publication_date:desc",
        "per-page": max_results,
    }


def _work_to_item(work: dict[str, Any], domain: DomainConfig) -> DigestItem:
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    openalex_url = work.get("id") or ""
    openalex_id = openalex_url.rsplit("/", 1)[-1] if openalex_url else str(work.get("id") or "")
    doi = _normalize_doi(work.get("doi"))
    landing_url = primary.get("landing_page_url") or openalex_url
    pdf_url = primary.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
    concepts = [str(item.get("display_name")) for item in work.get("concepts", []) or [] if item.get("display_name")]
    return DigestItem(
        id=openalex_id,
        source="openalex",
        item_type="paper",
        title=str(work.get("title") or "untitled"),
        url=landing_url,
        pdf_url=pdf_url,
        authors=[str(authorship.get("author", {}).get("display_name")) for authorship in work.get("authorships", []) or [] if authorship.get("author", {}).get("display_name")],
        abstract=_abstract_from_inverted_index(work.get("abstract_inverted_index")),
        published_at=work.get("publication_date"),
        updated_at=work.get("updated_date") or work.get("publication_date"),
        source_tags=["openalex", domain.name, *concepts],
        categories=concepts,
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "openalex_id": openalex_id,
            "venue": source.get("display_name"),
            "cited_by_count": work.get("cited_by_count"),
            "open_access_url": pdf_url,
            "openalex_url": openalex_url,
            "publication_type": work.get("type"),
        },
    )


def _abstract_from_inverted_index(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    positions = []
    for word, indexes in value.items():
        for index in indexes or []:
            positions.append((int(index), str(word)))
    return " ".join(word for _, word in sorted(positions)) if positions else None


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


def _get_with_retry(client: httpx.Client, params: dict[str, str | int], source_config: dict) -> httpx.Response:
    retries = max(0, int(source_config.get("rate_limit_retries", 2)))
    backoff = float(source_config.get("rate_limit_backoff_seconds", 2))
    last = None
    for attempt in range(retries + 1):
        response = client.get(OPENALEX_WORKS_URL, params=params)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        last = response
        if attempt < retries:
            time.sleep(min(backoff * (2 ** attempt), 20))
    if last is not None:
        last.raise_for_status()
    raise httpx.HTTPError("OpenAlex request failed")
