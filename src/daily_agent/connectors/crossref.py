from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.publication_types import allowed_publication_types, publication_type_allowed
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection
from daily_agent.connectors.official_metadata import MetadataBudget, discovery_bounds, failure_status, fetch_text

CROSSREF_WORKS_URL = "https://api.crossref.org/works"


def fetch_preferred_journals(config: AppConfig, target_date: datetime | None = None,
                             window_days: int | None = None, *, coverage: dict | None = None,
                             budget: MetadataBudget | None = None) -> list[DigestItem]:
    """Bounded, targeted journal leads; neither archive enumeration nor verification.

    This pass is independently callable before any broad Crossref query. Queries
    are interleaved across journals so one journal cannot consume the whole cap.
    """
    settings = config.sources.get("crossref", {}) or {}
    report = coverage if coverage is not None else {}
    report.update(mode="targeted_journal_queries", complete=False, partial_recall=True,
                  requests=0, host_blocked=False)
    if not settings.get("enabled", False):
        report["reason"] = "disabled"
        return []
    target = target_date or datetime.now(timezone.utc)
    days = max(0, int(window_days if window_days is not None else settings.get("recent_days", 7)))
    bounds = discovery_bounds(config, target, days)
    cap = min(16, max(0, int(settings.get("preferred_max_requests", 16))))
    rows = min(100, max(1, int(settings.get("max_results_per_query", 10))))
    timeout = min(30.0, max(0.1, float(settings.get("preferred_timeout_seconds", 15))))
    budget = budget or MetadataBudget(max_requests=cap, max_bytes=16_000_000, seconds=120)
    initial_requests = budget.requests
    queries = []
    per_domain = max(0, int(settings.get("preferred_queries_per_domain", 1)))
    for domain in config.domains:
        for query in _queries(domain)[:per_domain]:
            if query not in {row[0] for row in queries}:
                queries.append((query, domain))
    journals = []
    for journal in settings.get("preferred_journals", []) or []:
        issn = str(journal.get("issn") or "")
        if re.fullmatch(r"\d{4}-\d{3}[\dX]", issn) and issn not in journals:
            journals.append(issn)
    scheduled = [(issn, query, domain) for query, domain in queries for issn in journals]
    report.update(start_date=bounds[0].isoformat(), end_date=bounds[1].isoformat(),
                  scheduled_queries=len(scheduled), request_limit=cap)
    items, failures, errors = [], [], []
    successful_requests = 0
    with httpx.Client(timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for issn, query, domain in scheduled[:cap]:
            params = _params(query, target, days, rows, bounds=bounds)
            params["filter"] += ",type:journal-article"
            url = f"https://api.crossref.org/journals/{issn}/works?{urlencode(params)}"
            try:
                text, _, _ = fetch_text(client, url, budget, 2_000_000,
                    _validate_journal_url, expected_types=("application/json",))
                payload = json.loads(text)
                if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
                    raise ValueError("Expected Crossref message object")
                works = payload["message"].get("items", [])
                if not isinstance(works, list):
                    raise ValueError("Expected Crossref items array")
                successful_requests += 1
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failures.append(failure_record(f"journal:{issn} query:{query}", exc))
                if failure_status(exc) in {"blocked", "rate_limited"}:
                    report.update(host_blocked=True, reason="host_access_denied_or_rate_limited")
                    break
                # A shared budget exhaustion must not keep trying unsent queries.
                if "budget" in str(exc).casefold() or "deadline" in str(exc).casefold():
                    report["reason"] = "budget_exhausted"
                    break
                continue
            for work in works:
                if isinstance(work, dict) and work.get("type") == "journal-article":
                    item = _work_to_item(work, domain)
                    item.raw.update(discovery_source="crossref_preferred_journal",
                                    discovery_issn=issn, discovery_query=query,
                                    discovery_partial_recall=True)
                    items.append(item)
    report.setdefault("reason", "request_limit" if len(scheduled) > cap else "targeted_queries_only")
    report["failures"] = failures
    report["requests"] = budget.requests - initial_requests
    unique = list({item.canonical_key(): item for item in items}.values())
    return finish_collection("Crossref preferred journals", unique, failures, errors, successful_requests)


def _validate_journal_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.crossref.org"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not re.fullmatch(r"/journals/\d{4}-\d{3}[\dX]/works", parsed.path)):
        raise ValueError("Expected an HTTPS Crossref journal metadata URL")


def fetch_crossref(config: AppConfig, target_date: datetime | None = None,
                   window_days: int | None = None, *, preferred_items: list[DigestItem] | None = None,
                   preferred_coverage: dict | None = None) -> list[DigestItem]:
    source_config = config.sources.get("crossref", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days if window_days is not None else source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    bounds = discovery_bounds(config, target, days)
    max_results = int(source_config.get("max_results_per_query", 10))
    timeout = float(source_config.get("timeout_seconds", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    allowed_types = allowed_publication_types(source_config)
    items: list[DigestItem] = list(preferred_items or [])
    failures, errors = [], []
    successful_requests = int(bool(items))
    report = preferred_coverage if preferred_coverage is not None else {}
    # Formal-journal discovery always gets its bounded opportunity first.
    # Passing a pre-fetched list, including [], avoids a duplicate journal pass.
    if preferred_items is None:
        try:
            items.extend(fetch_preferred_journals(config, target, window_days, coverage=report))
            successful_requests += int(report.get("requests", 0) > 0)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            items.extend(getattr(exc, "partial_items", []))
            failures.extend(getattr(exc, "failures", []) or [failure_record("preferred journals", exc)])
            errors.append(exc)
            successful_requests += int(bool(items) or getattr(exc, "partial_success", False))
    if report.get("host_blocked"):
        if not errors:
            # An external caller supplied the completed pass and its cooldown.
            return list({item.canonical_key(): item for item in items}.values())
        return finish_collection("Crossref", items, failures, errors, successful_requests)
    seen_queries: set[str] = set()
    from daily_agent.author_context import watchlist_discovery_queries
    watch_queries = watchlist_discovery_queries(config, limit=min(2, max(0, int(source_config.get("watchlist_query_slots", 2)))))
    with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for domain in config.domains:
            queries = _queries(domain)
            if max_queries_per_domain > 0:
                queries = queries[:max_queries_per_domain]
            slots = min(len(watch_queries), max(0, len(queries) - 1))
            if slots:
                queries = [*queries[:-slots], *watch_queries[:slots]]
                watch_queries = watch_queries[slots:]
            for query in queries:
                if query in seen_queries:
                    continue
                seen_queries.add(query)
                try:
                    response = client.get(CROSSREF_WORKS_URL, params=_params(query, target, days, max_results, bounds=bounds))
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Expected source JSON object")
                    successful_requests += 1
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(exc)
                    failures.append(failure_record(query, exc))
                    if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                        return finish_collection("Crossref", items, failures, errors, successful_requests)
                    continue
                for work in payload.get("message", {}).get("items", []) or []:
                    if publication_type_allowed(work.get("type"), allowed_types):
                        items.append(_work_to_item(work, domain))
    return finish_collection("Crossref", list({item.canonical_key(): item for item in items}.values()), failures, errors, successful_requests)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _params(query: str, target: datetime, days: int, max_results: int, *,
            bounds: tuple[date, date] | None = None) -> dict[str, str | int]:
    start = (bounds[0] if bounds else (target - timedelta(days=days)).date()).isoformat()
    end = (bounds[1] if bounds else target.date()).isoformat()
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
    date_field = next((key for key in ("published-online", "published-print", "published", "issued")
                       if work.get(key)), None)
    published_at = _date_parts(work.get(date_field)) if date_field else None
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
            "publication_date_basis": date_field,
            "discovery_primary_url": next((str(link.get("URL")) for link in ((work.get("resource") or {}).get("primary", {}),)
                                            if isinstance(link, dict) and link.get("URL")), None),
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
