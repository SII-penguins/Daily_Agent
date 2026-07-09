from __future__ import annotations

import re
from typing import Any

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import MaterialRecord

OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def enrich_citation_contexts(records: list[MaterialRecord], config: AppConfig) -> list[MaterialRecord]:
    source_config = config.sources.get("citation_context", {}) or {}
    if not source_config.get("enabled", True):
        return records
    max_papers = int(source_config.get("max_papers_per_run", 10))
    max_citing = int(source_config.get("max_citing_papers", 3))
    max_referenced = int(source_config.get("max_referenced_papers", 3))
    timeout = float(source_config.get("timeout_seconds", 30))
    if max_papers <= 0:
        return records
    candidates = [
        record
        for record in records
        if record.item_type == "paper" and _openalex_lookup_path(record) and not (record.raw or {}).get("citation_context")
    ][:max_papers]
    if not candidates:
        return records
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for record in candidates:
            context = _fetch_openalex_context(client, record, max_citing=max_citing, max_referenced=max_referenced)
            if context:
                record.raw["citation_context"] = context
    return records


def _fetch_openalex_context(client: httpx.Client, record: MaterialRecord, max_citing: int, max_referenced: int) -> dict[str, Any] | None:
    lookup_path = _openalex_lookup_path(record)
    if not lookup_path:
        return None
    try:
        response = client.get(f"{OPENALEX_WORKS_URL}/{lookup_path}")
        response.raise_for_status()
        work = response.json()
    except httpx.HTTPError:
        return None
    work_id = _short_openalex_id(work.get("id"))
    if not work_id:
        return None
    context: dict[str, Any] = {
        "source": "openalex",
        "work_id": work_id,
        "title": work.get("title") or record.title,
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "citing": _fetch_citing(client, work_id, max_citing),
        "referenced": _fetch_referenced(client, work.get("referenced_works") or [], max_referenced),
    }
    return context


def _fetch_citing(client: httpx.Client, work_id: str, max_items: int) -> list[dict[str, Any]]:
    if max_items <= 0:
        return []
    try:
        response = client.get(
            OPENALEX_WORKS_URL,
            params={"filter": f"cites:{work_id}", "per-page": max_items, "sort": "cited_by_count:desc"},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    return [_work_summary(work) for work in response.json().get("results", []) or [] if work.get("title")]


def _fetch_referenced(client: httpx.Client, referenced_works: list[Any], max_items: int) -> list[dict[str, Any]]:
    ids = [_short_openalex_id(value) for value in referenced_works]
    ids = [value for value in ids if value][:max_items]
    if not ids:
        return []
    try:
        response = client.get(OPENALEX_WORKS_URL, params={"filter": f"openalex:{'|'.join(ids)}", "per-page": len(ids)})
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    works = [_work_summary(work) for work in response.json().get("results", []) or [] if work.get("title")]
    works.sort(key=lambda item: int(item.get("cited_by_count") or 0), reverse=True)
    return works[:max_items]


def _openalex_lookup_path(record: MaterialRecord) -> str | None:
    if record.doi:
        return f"doi:{record.doi.lower()}"
    for value in [
        (record.source_aliases or {}).get("openalex"),
        (record.raw or {}).get("openalex_id"),
        (record.raw or {}).get("openalex_url"),
    ]:
        work_id = _short_openalex_id(value)
        if work_id:
            return work_id
    arxiv_id = _arxiv_identifier(record)
    if arxiv_id:
        return f"doi:10.48550/arxiv.{arxiv_id.lower()}"
    return None


def _arxiv_identifier(record: MaterialRecord) -> str | None:
    values = [
        record.key.split(":", 1)[1] if record.key.startswith("arxiv:") else None,
        (record.source_aliases or {}).get("arxiv"),
        (record.raw or {}).get("arxiv_id"),
    ]
    for value in values:
        if not value:
            continue
        text = str(value).strip().removeprefix("arXiv:").removeprefix("arxiv:")
        text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
        if re.match(r"^\d{4}\.\d{4,5}$", text):
            return text
    return None


def _short_openalex_id(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1]


def _work_summary(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _short_openalex_id(work.get("id")),
        "title": str(work.get("title") or ""),
        "year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count") or 0,
        "doi": _normalize_doi(work.get("doi")),
        "url": work.get("id"),
    }


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    text = text.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    return text.lower() or None
