from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.publication_types import allowed_publication_types, publication_type_allowed
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection

CROSSREF_WORKS_URL = "https://api.crossref.org/works"


def fetch_crossref(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("crossref", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    allowed_types = allowed_publication_types(source_config)
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    seen_queries: set[str] = set()
    from daily_agent.author_context import watchlist_discovery_queries
    watch_queries = watchlist_discovery_queries(config, limit=min(2, max(0, int(source_config.get("watchlist_query_slots", 2)))))
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            # Replace slots, never append requests; retain at least one core
            # topical query. Candidate hits still pass normal date/topic gates.
            slots = min(len(watch_queries), max(0, len(queries) - 1))
            if slots:
                queries = [*queries[:-slots], *watch_queries[:slots]]
                watch_queries = watch_queries[slots:]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(CROSSREF_WORKS_URL, params=_params(query, target, days, max_results))
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict): raise ValueError("Expected source JSON object")
                    successful_requests += 1
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(exc)
                    failures.append(failure_record(query, exc))
                    if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                        return finish_collection("Crossref", items, failures, errors, successful_requests)
                    continue
                for work in payload.get("message", {}).get("items", []) or []:
                    if not publication_type_allowed(work.get("type"), allowed_types):
                        continue
                    items.append(_work_to_item(work, domain))
        # Journal-scoped retrieval prevents the broad query budget from being
        # consumed entirely by preprints or unrelated publishers.
        preferred = source_config.get("preferred_journals", [])
        cap = int(source_config.get("preferred_queries_per_domain", 1))
        for journal in preferred:
            issn = str(journal.get("issn") or "")
            if not re.fullmatch(r"\d{4}-\d{3}[\dX]", issn):
                continue
            journal_queries = set()
            for domain in config.domains:
                for query in _queries(domain)[:max(0, cap)]:
                    if query in journal_queries: continue
                    journal_queries.add(query)
                    params = _params(query, target, int(source_config.get("preferred_recent_days", days)), max_results)
                    params["filter"] += ",type:journal-article"
                    try:
                        response = client.get(f"https://api.crossref.org/journals/{issn}/works", params=params)
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict): raise ValueError("Expected source JSON object")
                        successful_requests += 1
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append(exc)
                        failures.append(failure_record(f"journal:{issn} query:{query}", exc))
                        if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                            return finish_collection("Crossref", items, failures, errors, successful_requests)
                        continue
                    for work in payload.get("message", {}).get("items", []) or []:
                        if work.get("type") == "journal-article":
                            items.append(_work_to_item(work, domain))
    return finish_collection("Crossref", list({item.canonical_key(): item for item in items}.values()), failures, errors, successful_requests)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


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
            "authorships": [{"name": _author_name(a), "orcid": a.get("ORCID"), "institutions": [i.get("name", "") for i in a.get("affiliation", []) if i.get("name")]} for a in work.get("author", []) or []],
            "venue": venue,
            "publisher": work.get("publisher"),
            "cited_by_count": work.get("is-referenced-by-count"),
            "publication_type": work.get("type"),
            "publication_date_precision": {4: "year", 7: "month", 10: "day"}.get(len(published_at or ""), "unknown"),
        },
    )


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value) if value else None


def _date_parts(value: Any) -> str | None:
    # Preserve source precision: padding year/month metadata would manufacture
    # an exact date and incorrectly admit it through the discovery-window gate.
    parts = value.get("date-parts") if isinstance(value, dict) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None
    fields = parts[0]
    if not 1 <= len(fields) <= 3 or any(type(part) is not int for part in fields):
        return None
    try:
        # Defaults validate the supplied components only; never publish them.
        date(*([*fields, 1, 1][:3]))
    except ValueError:
        return None
    return "-".join(f"{part:04d}" if index == 0 else f"{part:02d}"
                    for index, part in enumerate(fields))


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
