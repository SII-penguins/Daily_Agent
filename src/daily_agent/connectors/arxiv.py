from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection

ARXIV_API_URL = "https://export.arxiv.org/api/query"
_ARXIV_ID_RE = re.compile(r"/abs/([^/?#]+)")
_VERSION_RE = re.compile(r"v(\d+)$")


def fetch_arxiv(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("arxiv", {}) or {}
    if not source_config.get("enabled", True):
        return []

    target = target_date or datetime.now(timezone.utc)
    recent_days = int(source_config.get("recent_days", 7))
    query_window_days = int(window_days or recent_days)
    max_results = int(source_config.get("max_results_per_query", 25))
    delay = float(source_config.get("request_delay_seconds", 3))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)

    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    failures, errors = [], []
    successful_requests = 0
    rate_limited = False
    for domain in config.domains:
        queries = _domain_queries(domain)
        if max_queries_per_domain > 0:
            queries = queries[:max_queries_per_domain]
        for query in queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            try:
                items.extend(_fetch_query(query, domain, target, recent_days, query_window_days, max_results, source_config))
                successful_requests += 1
            except httpx.HTTPStatusError as exc:
                errors.append(exc)
                failures.append(failure_record(query, exc))
                if exc.response.status_code in {401, 403, 429}:
                    rate_limited = True
                    break
            except (httpx.HTTPError, ET.ParseError) as exc:
                errors.append(exc)
                failures.append(failure_record(query, exc))
            if delay > 0:
                time.sleep(delay)
        if rate_limited:
            break
    return finish_collection("arXiv", items, failures, errors, successful_requests)


def _domain_queries(domain: DomainConfig) -> list[str]:
    category_terms = [f"cat:{category}" for category in domain.arxiv_categories]
    keyword_terms = []
    for keyword in domain.include_keywords:
        escaped = quote_plus(f'"{keyword}"') if " " in keyword else quote_plus(keyword)
        keyword_terms.append(f"all:{escaped}")

    queries = []
    if category_terms and keyword_terms:
        category_query = "+OR+".join(category_terms)
        for keyword_query in keyword_terms:
            queries.append(f"({category_query})+AND+{keyword_query}")
    elif category_terms:
        queries.append("+OR+".join(category_terms))
    elif keyword_terms:
        queries.extend(keyword_terms)
    return queries


def _fetch_query(
    search_query: str,
    domain: DomainConfig,
    target: datetime,
    recent_days: int,
    query_window_days: int,
    max_results: int,
    source_config: dict,
) -> list[DigestItem]:
    params = {
        "search_query": _submitted_date_range_query(search_query, target, query_window_days),
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_URL}?" + "&".join(f"{key}={value}" for key, value in params.items())
    response = _get_with_retry(url, source_config)
    root = ET.fromstring(response.text)
    cutoff = target - timedelta(days=recent_days)
    items = []
    for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
        item = _entry_to_item(entry, domain)
        if _parse_datetime(item.updated_at) and _parse_datetime(item.updated_at) < cutoff:
            item.is_historical_supplement = True
        item.quota_group = domain.quota_group
        items.append(item)
    return items


def _get_with_retry(url: str, source_config: dict) -> httpx.Response:
    max_retries = int(source_config.get("rate_limit_retries", 2))
    backoff_seconds = float(source_config.get("rate_limit_backoff_seconds", 20))
    max_backoff_seconds = float(source_config.get("rate_limit_max_backoff_seconds", 90))
    last_response: httpx.Response | None = None
    endpoints = [url]
    if "export.arxiv.org" in url:
        endpoints.append(url.replace("export.arxiv.org", "arxiv.org"))
    last_error: Exception | None = None
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for endpoint in endpoints:
            try:
                for attempt in range(max_retries + 1):
                    response = client.get(endpoint)
                    if response.status_code != 429:
                        response.raise_for_status()
                        return response
                    last_response = response
                    if attempt >= max_retries:
                        response.raise_for_status()
                    time.sleep(_retry_after_seconds(response, attempt, backoff_seconds, max_backoff_seconds))
            except httpx.HTTPError as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                    raise  # Never switch hosts to evade access denial or rate limits.
                last_error = exc
                continue
    if last_response is not None:
        last_response.raise_for_status()
    if last_error is not None:
        raise last_error
    raise httpx.HTTPError("arXiv request failed")


def _retry_after_seconds(response: httpx.Response, attempt: int, backoff_seconds: float, max_backoff_seconds: float) -> float:
    fallback = min(backoff_seconds * (2**attempt), max_backoff_seconds)
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return fallback
    try:
        return min(max(float(retry_after), 0), max_backoff_seconds)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, IndexError, OverflowError):
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return min(max((parsed - datetime.now(timezone.utc)).total_seconds(), 0), max_backoff_seconds)


def _submitted_date_range_query(base_query: str, target: datetime, window_days: int) -> str:
    start = _format_arxiv_datetime(target - timedelta(days=window_days))
    end = _format_arxiv_datetime(target + timedelta(days=1))
    return f"({base_query})+AND+submittedDate:[{start}+TO+{end}]"


def _format_arxiv_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def _entry_to_item(entry: ET.Element, domain: DomainConfig) -> DigestItem:
    entry_id = _text(entry, "id")
    arxiv_id_full = _extract_arxiv_id(entry_id)
    arxiv_id = _strip_version(arxiv_id_full)
    version = _extract_version(arxiv_id_full)
    abs_url = f"https://arxiv.org/abs/{arxiv_id_full}" if arxiv_id_full else entry_id
    pdf_url = _pdf_link(entry) or (f"https://arxiv.org/pdf/{arxiv_id_full}" if arxiv_id_full else None)
    authors = [_clean_text(_child_text(author, "name")) for author in entry.findall("{http://www.w3.org/2005/Atom}author")]
    categories = [category.attrib.get("term", "") for category in entry.findall("{http://www.w3.org/2005/Atom}category") if category.attrib.get("term")]
    published_at = _iso_datetime(_text(entry, "published"))
    updated_at = _iso_datetime(_text(entry, "updated"))
    summary = _clean_text(_text(entry, "summary"))
    title = _clean_text(_text(entry, "title"))
    return DigestItem(
        id=arxiv_id or arxiv_id_full or entry_id,
        source="arxiv",
        item_type="paper",
        title=title,
        url=abs_url,
        pdf_url=pdf_url,
        authors=authors,
        abstract=summary,
        published_at=published_at,
        updated_at=updated_at,
        source_tags=[domain.name],
        categories=categories,
        arxiv_id=arxiv_id,
        arxiv_version=version,
        doi=_text(entry, "doi", namespace="http://arxiv.org/schemas/atom") or None,
        quota_group=domain.quota_group,
        raw={"entry_id": entry_id, "domain": domain.name,
             "journal_ref": _text(entry, "journal_ref", namespace="http://arxiv.org/schemas/atom") or None},
    )


def _extract_arxiv_id(value: str) -> str:
    match = _ARXIV_ID_RE.search(value)
    if match:
        return match.group(1)
    return value.rsplit("/", 1)[-1]


def _strip_version(arxiv_id: str) -> str:
    return _VERSION_RE.sub("", arxiv_id or "")


def _extract_version(arxiv_id: str) -> str | None:
    match = _VERSION_RE.search(arxiv_id or "")
    return match.group(0) if match else None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_datetime(value: str) -> str | None:
    parsed = _parse_datetime(value)
    if not parsed:
        return value or None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _text(entry: ET.Element, tag: str, namespace: str = "http://www.w3.org/2005/Atom") -> str:
    child = entry.find(f"{{{namespace}}}{tag}")
    return child.text.strip() if child is not None and child.text else ""


def _child_text(entry: ET.Element, tag: str) -> str:
    child = entry.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    return child.text.strip() if child is not None and child.text else ""


def _pdf_link(entry: ET.Element) -> str | None:
    for link in entry.findall("{http://www.w3.org/2005/Atom}link"):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            return link.attrib.get("href")
    return None


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
