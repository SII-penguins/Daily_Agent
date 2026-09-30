from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.connectors.queries import scholarly_queries
from daily_agent.models import DigestItem
from daily_agent.secrets import credential_value

SERPAPI_GOOGLE_SCHOLAR_URL = "https://serpapi.com/search.json"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
SCHOLARLY_RUNTIME_ENV = "DAILY_AGENT_SUPERVISED_SCHOLARLY_RUNTIME"


class _ScholarlyRuntimeTimeout(BaseException):
    pass


def fetch_google_scholar(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("google_scholar", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    provider = str(source_config.get("provider") or "serpapi").lower()
    env_name = str(source_config.get("api_key_env") or "SERPAPI_API_KEY")
    api_key = credential_value(env_name)
    if provider in {"serpapi", "auto"} and api_key:
        return _fetch_serpapi(config, target, days, source_config, api_key)
    if scholarly_runtime_enabled(source_config) and provider in {"scholarly", "auto", "serpapi"}:
        return _fetch_scholarly_with_budget(config, target, days, source_config)
    return []


def google_scholar_skip_reason(config: AppConfig) -> str | None:
    source_config = config.sources.get("google_scholar", {}) or {}
    if not source_config.get("enabled", False):
        return "disabled"
    env_name = str(source_config.get("api_key_env") or "SERPAPI_API_KEY")
    provider = str(source_config.get("provider") or "serpapi").lower()
    if provider in {"serpapi", "auto"} and credential_value(env_name):
        return None
    if scholarly_runtime_enabled(source_config) and scholarly_available():
        return None
    if source_config.get("scholarly_fallback_enabled", False) and scholarly_available():
        return f"missing {env_name}; scholarly runtime fallback disabled"
    return f"missing {env_name} and scholarly package"


def scholarly_available() -> bool:
    return importlib.util.find_spec("scholarly") is not None


def scholarly_runtime_enabled(source_config: dict[str, Any]) -> bool:
    env_enabled = str(os.environ.get(SCHOLARLY_RUNTIME_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}
    return bool(source_config.get("scholarly_fallback_enabled", False) and (source_config.get("scholarly_runtime_enabled", False) or env_enabled))


def _fetch_serpapi(config: AppConfig, target: datetime, days: int, source_config: dict[str, Any], api_key: str) -> list[DigestItem]:
    max_results = int(source_config.get("max_results_per_query", 20))
    timeout = float(source_config.get("timeout_seconds", 45))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    sort_by_date = bool(source_config.get("sort_by_date", True))
    scisbd = int(source_config.get("scisbd", 2))
    start_year = (target - timedelta(days=days)).year
    end_year = target.year
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
                for start, page_size in _serpapi_pages(max_results):
                    params = {
                        "engine": "google_scholar",
                        "q": query,
                        "api_key": api_key,
                        "num": page_size,
                        "as_ylo": start_year,
                        "as_yhi": end_year,
                    }
                    if start:
                        params["start"] = start
                    if sort_by_date:
                        params["scisbd"] = scisbd
                    try:
                        response = client.get(SERPAPI_GOOGLE_SCHOLAR_URL, params=params)
                        response.raise_for_status()
                    except httpx.HTTPError:
                        break
                    results = response.json().get("organic_results", []) or []
                    if not results:
                        break
                    for result in results:
                        item = _serpapi_result_to_item(result, domain, query)
                        if item:
                            items.append(item)
        items = _dedupe(items)
        _enrich_serpapi_citations(client, items, source_config, api_key, config)
    return items


def _fetch_scholarly(config: AppConfig, target: datetime, days: int, source_config: dict[str, Any]) -> list[DigestItem]:
    try:
        from scholarly import scholarly as sch  # type: ignore
    except Exception:
        return []
    max_results = int(source_config.get("scholarly_max_results_per_query", source_config.get("max_results_per_query", 10)))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    cutoff_year = (target - timedelta(days=days)).year
    items: list[DigestItem] = []
    seen_queries: set[str] = set()
    for domain in config.domains:
        queries = _queries(domain)
        if max_queries_per_domain > 0:
            queries = queries[:max_queries_per_domain]
        for query in queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            try:
                results = sch.search_pubs(query)
                for result in islice(results, max_results):
                    item = _scholarly_result_to_item(result, domain, query)
                    if item and (not item.published_at or int(item.published_at[:4]) >= cutoff_year):
                        items.append(item)
            except Exception:
                continue
    return _dedupe(items)


def _fetch_scholarly_with_budget(config: AppConfig, target: datetime, days: int, source_config: dict[str, Any]) -> list[DigestItem]:
    timeout = float(source_config.get("scholarly_runtime_timeout_seconds", source_config.get("timeout_seconds", 60)))
    if timeout <= 0 or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return _fetch_scholarly(config, target, days, source_config)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _timeout_handler(signum, frame):  # type: ignore[no-untyped-def]
        raise _ScholarlyRuntimeTimeout()

    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        return _fetch_scholarly(config, target, days, source_config)
    except _ScholarlyRuntimeTimeout:
        return []
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _queries(domain: DomainConfig) -> list[str]:
    return scholarly_queries(domain)


def _serpapi_pages(max_results: int) -> list[tuple[int, int]]:
    remaining = max(1, max_results)
    pages: list[tuple[int, int]] = []
    start = 0
    while remaining > 0:
        page_size = min(20, remaining)
        pages.append((start, page_size))
        start += page_size
        remaining -= page_size
    return pages


def _serpapi_result_to_item(result: dict[str, Any], domain: DomainConfig, query: str) -> DigestItem | None:
    title = str(result.get("title") or "").strip()
    if not title:
        return None
    publication = result.get("publication_info") or {}
    summary = str(publication.get("summary") or "")
    link = result.get("link") or _scholar_query_url(title)
    snippet = str(result.get("snippet") or "")
    inline_links = result.get("inline_links") or {}
    cited_by = inline_links.get("cited_by") or {}
    versions = inline_links.get("versions") or {}
    result_id = str(result.get("result_id") or _stable_result_id(title, link))
    pdf_urls = _pdf_urls(result)
    pdf_url = pdf_urls[0] if pdf_urls else None
    year = _year(summary) or _year(snippet)
    doi = _extract_doi(" ".join([link, title, snippet, summary]))
    return DigestItem(
        id=result_id,
        source="google_scholar",
        item_type="paper",
        title=title,
        url=link,
        pdf_url=pdf_url,
        authors=_authors(publication),
        abstract=snippet or None,
        published_at=year,
        updated_at=year,
        source_tags=["google_scholar", domain.name, query],
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "google_scholar_id": result_id,
            "google_scholar_url": result.get("serpapi_scholar_link") or _scholar_query_url(title),
            "venue": _venue(summary),
            "citation_count": cited_by.get("total"),
            "google_scholar_cited_by_url": cited_by.get("link"),
            "google_scholar_cites_id": cited_by.get("cites_id"),
            "google_scholar_related_url": inline_links.get("related_pages_link"),
            "google_scholar_versions_url": versions.get("link"),
            "google_scholar_versions_count": versions.get("total"),
            "google_scholar_cluster_id": versions.get("cluster_id"),
            "google_scholar_cached_url": inline_links.get("cached_page_link"),
            "serpapi_cite_url": inline_links.get("serpapi_cite_link"),
            "serpapi_related_pages_url": inline_links.get("serpapi_related_pages_link"),
            "serpapi_versions_url": versions.get("serpapi_scholar_link"),
            "publication_summary": summary,
            "pdf_urls": pdf_urls,
        },
    )


def _enrich_serpapi_citations(client: httpx.Client, items: list[DigestItem], source_config: dict[str, Any], api_key: str, config: AppConfig) -> None:
    if not source_config.get("cite_enrichment_enabled", False):
        return
    max_items = int(source_config.get("cite_enrichment_max_results_per_run", 0) or 0)
    if max_items <= 0:
        return
    cache_enabled = bool(source_config.get("cite_cache_enabled", True))
    cache_path = _cite_cache_path(config, source_config)
    cache = _load_cite_cache(cache_path) if cache_enabled else {}
    cache_changed = False
    network_enriched = 0
    run_budget = float(source_config.get("cite_enrichment_run_budget_seconds", 45) or 0)
    deadline = time.monotonic() + run_budget if run_budget > 0 else None
    for item in items:
        scholar_id = str(item.raw.get("google_scholar_id") or "").strip()
        if not scholar_id or item.raw.get("google_scholar_citation_formats"):
            continue
        cache_entry = (cache.get("items", {}) or {}).get(scholar_id) if cache_enabled else None
        if _apply_cite_cache_entry(item, cache_entry):
            continue
        if network_enriched >= max_items:
            break
        request_timeout = float(source_config.get("timeout_seconds", 60))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request_timeout = min(request_timeout, remaining)
        try:
            response = client.get(
                SERPAPI_GOOGLE_SCHOLAR_URL,
                params={"engine": "google_scholar_cite", "q": scholar_id, "api_key": api_key},
                timeout=request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        if _apply_cite_payload(item, payload):
            network_enriched += 1
            if cache_enabled:
                _store_cite_cache_entry(cache, scholar_id, item, max_entries=int(source_config.get("cite_cache_max_entries", 50_000)))
                cache_changed = True
    if cache_changed:
        _write_cite_cache(cache_path, cache)


def _apply_cite_payload(item: DigestItem, payload: dict[str, Any]) -> bool:
    citations, links = _parse_cite_payload(payload)
    return _apply_cite_data(item, citations, links, status="resolved")


def _parse_cite_payload(payload: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    citations = {}
    for citation in payload.get("citations", []) or []:
        if not isinstance(citation, dict):
            continue
        title = str(citation.get("title") or "").strip()
        snippet = str(citation.get("snippet") or "").strip()
        if title and snippet:
            citations[title] = snippet
    links = {}
    for link in payload.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        name = str(link.get("name") or "").strip()
        url = str(link.get("link") or "").strip()
        if name and url:
            links[name] = url
    return citations, links


def _apply_cite_data(item: DigestItem, citations: dict[str, str], links: dict[str, str], status: str) -> bool:
    if citations:
        item.raw["google_scholar_citation_formats"] = citations
    if links:
        item.raw["google_scholar_citation_export_links"] = links
        for name, raw_key in [
            ("BibTeX", "google_scholar_bibtex_url"),
            ("EndNote", "google_scholar_endnote_url"),
            ("RefMan", "google_scholar_refman_url"),
            ("RefWorks", "google_scholar_refworks_url"),
        ]:
            if links.get(name):
                item.raw[raw_key] = links[name]
    if citations or links:
        item.raw["google_scholar_cite_enrichment"] = {"status": status}
        return True
    return False


def _apply_cite_cache_entry(item: DigestItem, entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    citations = entry.get("citations") if isinstance(entry.get("citations"), dict) else {}
    links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
    citations = {str(key): str(value) for key, value in citations.items() if key and value}
    links = {str(key): str(value) for key, value in links.items() if key and value}
    return _apply_cite_data(item, citations, links, status="cache_hit")


def _cite_cache_path(config: AppConfig, source_config: dict[str, Any]) -> Path:
    configured = str(source_config.get("cite_cache_path") or "data/cache/google_scholar_cites.json")
    path = Path(configured)
    return path if path.is_absolute() else config.root / path


def _load_cite_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "items": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "items": {}}
    if not isinstance(payload.get("items"), dict):
        payload["items"] = {}
    payload.setdefault("schema_version", 1)
    return payload


def _store_cite_cache_entry(cache: dict[str, Any], scholar_id: str, item: DigestItem, max_entries: int) -> None:
    if not scholar_id:
        return
    citations = item.raw.get("google_scholar_citation_formats")
    links = item.raw.get("google_scholar_citation_export_links")
    if not isinstance(citations, dict) and not isinstance(links, dict):
        return
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items
    items[scholar_id] = {
        "saved_at": _utc_now(),
        "citations": citations if isinstance(citations, dict) else {},
        "links": links if isinstance(links, dict) else {},
    }
    if max_entries > 0 and len(items) > max_entries:
        for key in sorted(items, key=lambda item_key: str((items.get(item_key) or {}).get("saved_at") or ""))[: len(items) - max_entries]:
            items.pop(key, None)


def _write_cite_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**cache, "schema_version": 1, "updated_at": _utc_now()}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _scholarly_result_to_item(result: dict[str, Any], domain: DomainConfig, query: str) -> DigestItem | None:
    bib = result.get("bib") or {}
    title = str(bib.get("title") or "").strip()
    if not title:
        return None
    link = result.get("pub_url") or result.get("eprint_url") or _scholar_query_url(title)
    year = str(bib.get("pub_year") or "")
    result_id = str(result.get("author_id") or result.get("cites_id") or _stable_result_id(title, link))
    abstract = bib.get("abstract")
    doi = _extract_doi(" ".join(str(part or "") for part in [link, title, abstract]))
    return DigestItem(
        id=result_id,
        source="google_scholar",
        item_type="paper",
        title=title,
        url=link,
        pdf_url=result.get("eprint_url"),
        authors=_split_authors(str(bib.get("author") or "")),
        abstract=abstract,
        published_at=year or None,
        updated_at=year or None,
        source_tags=["google_scholar", "scholarly", domain.name, query],
        doi=doi,
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "google_scholar_id": result_id,
            "google_scholar_url": _scholar_query_url(title),
            "venue": bib.get("venue") or bib.get("journal"),
            "citation_count": result.get("num_citations"),
        },
    )


def _pdf_urls(result: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for resource in result.get("resources", []) or []:
        title = str(resource.get("title") or "").lower()
        file_format = str(resource.get("file_format") or "").lower()
        link = resource.get("link")
        if link and ("pdf" in title or file_format == "pdf") and link not in urls:
            urls.append(str(link))
    return urls


def _authors(publication: dict[str, Any]) -> list[str]:
    raw_authors = publication.get("authors") or []
    authors = [str(author.get("name")) for author in raw_authors if isinstance(author, dict) and author.get("name")]
    if authors:
        return authors
    summary = str(publication.get("summary") or "")
    names = summary.split(" - ", 1)[0]
    return _split_authors(names)


def _split_authors(value: str) -> list[str]:
    return [part.strip() for part in re.split(r",| and ", value) if part.strip()][:8]


def _year(value: str) -> str | None:
    match = re.search(r"\b(19|20)\d{2}\b", value or "")
    return match.group(0) if match else None


def _venue(summary: str) -> str | None:
    parts = [part.strip() for part in summary.split(" - ") if part.strip()]
    return parts[1] if len(parts) > 1 else None


def _extract_doi(value: str) -> str | None:
    match = DOI_RE.search(value or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;:)").lower()


def _scholar_query_url(title: str) -> str:
    return f"https://scholar.google.com/scholar?q={quote_plus(title)}"


def _stable_result_id(title: str, url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{title}-{url}".lower()).strip("-")[:96] or "google-scholar-result"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
