from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from daily_agent.config import AppConfig
from daily_agent.connectors.citation_context import OPENALEX_WORKS_URL, _openalex_lookup_path, _short_openalex_id
from daily_agent.models import MaterialRecord


def enrich_open_access_links(records: list[MaterialRecord], config: AppConfig) -> list[MaterialRecord]:
    source_config = config.sources.get("oa_resolver", {}) or {}
    if not source_config.get("enabled", True):
        return records
    max_papers = int(source_config.get("max_papers_per_run", 30))
    timeout = float(source_config.get("timeout_seconds", 30))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    if max_papers <= 0:
        return records
    candidates = [
        record
        for record in records
        if record.item_type == "paper"
        and _openalex_lookup_path(record)
        and not (record.raw or {}).get("openalex_oa_resolver")
        and (not record.pdf_url or not (record.raw or {}).get("open_access_url"))
    ][:max_papers]
    if not candidates:
        return records
    deadline = time.monotonic() + run_budget_seconds if run_budget_seconds > 0 else None
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for record in candidates:
            if _deadline_exceeded(deadline):
                _set_resolver_status(record, "skipped", reason="run_budget_exceeded")
                continue
            _resolve_record(client, record)
    return records


def _resolve_record(client: httpx.Client, record: MaterialRecord) -> None:
    lookup_path = _openalex_lookup_path(record)
    if not lookup_path:
        return
    try:
        response = client.get(f"{OPENALEX_WORKS_URL}/{lookup_path}")
        if response.status_code in {403, 404, 429}:
            _set_resolver_status(record, "unavailable", reason=f"http_{response.status_code}")
            return
        response.raise_for_status()
        work = response.json()
    except (httpx.HTTPError, ValueError):
        _set_resolver_status(record, "unavailable", reason="request_failed")
        return
    if not isinstance(work, dict):
        _set_resolver_status(record, "unavailable", reason="invalid_response")
        return
    links = _extract_links(work)
    if not any(links.values()):
        _set_resolver_status(record, "not_found")
        return
    if links.get("pdf_url") and not record.pdf_url:
        record.pdf_url = links["pdf_url"]
    if links.get("open_access_url"):
        record.raw["open_access_url"] = links["open_access_url"]
    if links.get("openalex_url"):
        record.raw["openalex_url"] = links["openalex_url"]
        short_id = _short_openalex_id(links["openalex_url"])
        if short_id:
            record.source_aliases.setdefault("openalex", short_id)
    if links.get("venue") and not record.raw.get("venue"):
        record.raw["venue"] = links["venue"]
    if links.get("landing_url") and record.url.startswith("https://doi.org/"):
        record.url = links["landing_url"]
    _merge_openalex_evidence(record, work, links)
    _set_resolver_status(record, "resolved", **{key: value for key, value in links.items() if value})


def _extract_links(work: dict[str, Any]) -> dict[str, str | None]:
    primary = _as_dict(work.get("primary_location"))
    best_oa = _as_dict(work.get("best_oa_location"))
    open_access = _as_dict(work.get("open_access"))
    locations = [_as_dict(value) for value in work.get("locations") or [] if isinstance(value, dict)]
    openalex_url = _string_or_none(work.get("id"))
    landing_url = (
        _string_or_none(primary.get("landing_page_url"))
        or _string_or_none(best_oa.get("landing_page_url"))
        or _first_location_value(locations, "landing_page_url")
    )
    pdf_url = (
        _string_or_none(primary.get("pdf_url"))
        or _string_or_none(best_oa.get("pdf_url"))
        or _first_location_value(locations, "pdf_url")
    )
    oa_url = _string_or_none(open_access.get("oa_url"))
    if not pdf_url and oa_url and _looks_like_pdf_url(oa_url):
        pdf_url = oa_url
    open_access_url = oa_url or pdf_url or landing_url
    venue = _source_name(primary) or _source_name(best_oa) or _first_location_source_name(locations)
    return {
        "openalex_url": openalex_url,
        "landing_url": landing_url,
        "pdf_url": pdf_url,
        "open_access_url": open_access_url,
        "venue": venue,
    }


def _merge_openalex_evidence(record: MaterialRecord, work: dict[str, Any], links: dict[str, str | None]) -> None:
    sources = record.evidence.setdefault("sources", {})
    existing = sources.get("openalex") if isinstance(sources.get("openalex"), dict) else {}
    sources["openalex"] = {
        **existing,
        "title": work.get("title") or record.title,
        "url": links.get("landing_url") or links.get("openalex_url") or existing.get("url"),
        "landing_url": links.get("landing_url") or existing.get("landing_url"),
        "pdf_url": links.get("pdf_url") or existing.get("pdf_url"),
        "open_access_url": links.get("open_access_url") or existing.get("open_access_url"),
        "doi": record.doi or _normalize_doi(work.get("doi")) or existing.get("doi"),
        "venue": links.get("venue") or existing.get("venue"),
        "citation_count": work.get("cited_by_count") or existing.get("citation_count"),
    }


def _set_resolver_status(record: MaterialRecord, status: str, **details: Any) -> None:
    record.raw["openalex_oa_resolver"] = {"status": status, **details}


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_location_value(locations: list[dict[str, Any]], key: str) -> str | None:
    for location in locations:
        value = _string_or_none(location.get(key))
        if value:
            return value
    return None


def _first_location_source_name(locations: list[dict[str, Any]]) -> str | None:
    for location in locations:
        name = _source_name(location)
        if name:
            return name
    return None


def _source_name(location: dict[str, Any]) -> str | None:
    source = location.get("source")
    if not isinstance(source, dict):
        return None
    return _string_or_none(source.get("display_name"))


def _looks_like_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".pdf") or "/pdf/" in path


def _normalize_doi(value: Any) -> str | None:
    text = _string_or_none(value)
    if not text:
        return None
    return text.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
