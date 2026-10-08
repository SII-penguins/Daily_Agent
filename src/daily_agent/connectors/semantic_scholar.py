from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.publication_types import allowed_publication_types, publication_type_allowed
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection
from daily_agent.secrets import credential_value

SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "paperId,title,abstract,url,year,publicationDate,venue,citationCount,influentialCitationCount,authors,externalIds,openAccessPdf,fieldsOfStudy,publicationTypes"


def fetch_semantic_scholar(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("semantic_scholar", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    allowed_types = allowed_publication_types(source_config)
    cutoff = (target - timedelta(days=days)).date().isoformat()
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    seen_queries: set[str] = set()
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=_headers(source_config)) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(SEMANTIC_SCHOLAR_SEARCH_URL, params={"query": query, "limit": max_results, "fields": FIELDS})
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict): raise ValueError("Expected source JSON object")
                    successful_requests += 1
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(exc)
                    failures.append(failure_record(query, exc))
                    if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                        return finish_collection("Semantic Scholar", items, failures, errors, successful_requests)
                    continue
                for paper in payload.get("data", []) or []:
                    if not publication_type_allowed(paper.get("publicationTypes"), allowed_types):
                        continue
                    item = _paper_to_item(paper, domain)
                    if not item.published_at or item.published_at >= cutoff:
                        items.append(item)
    return finish_collection("Semantic Scholar", items, failures, errors, successful_requests)


def _headers(source_config: dict[str, Any]) -> dict[str, str]:
    headers = {"User-Agent": "Daily-Agent/0.1"}
    env_name = str(source_config.get("api_key_env") or "SEMANTIC_SCHOLAR_API_KEY")
    token = credential_value(env_name)
    if token:
        headers["x-api-key"] = token
    return headers


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _paper_to_item(paper: dict[str, Any], domain: DomainConfig) -> DigestItem:
    external = paper.get("externalIds") or {}
    fields = [str(item) for item in paper.get("fieldsOfStudy", []) or [] if item]
    doi = _normalize_doi(external.get("DOI"))
    paper_id = str(paper.get("paperId") or "")
    return DigestItem(
        id=paper_id,
        source="semantic_scholar",
        item_type="paper",
        title=str(paper.get("title") or "untitled"),
        url=paper.get("url") or (f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""),
        pdf_url=(paper.get("openAccessPdf") or {}).get("url"),
        authors=[str(author.get("name")) for author in paper.get("authors", []) or [] if author.get("name")],
        abstract=paper.get("abstract"),
        published_at=paper.get("publicationDate") or (str(paper.get("year")) if paper.get("year") else None),
        updated_at=paper.get("publicationDate") or (str(paper.get("year")) if paper.get("year") else None),
        source_tags=["semantic_scholar", domain.name, *fields],
        categories=fields,
        arxiv_id=external.get("ArXiv"),
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "semantic_scholar_id": paper_id,
            "semantic_scholar_url": paper.get("url"),
            "venue": paper.get("venue"),
            "citation_count": paper.get("citationCount"),
            "influential_citation_count": paper.get("influentialCitationCount"),
            "publication_types": paper.get("publicationTypes") or [],
        },
    )


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    return str(value).removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
