from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from daily_agent.config import AppConfig
from daily_agent.connectors.citation_context import _openalex_lookup_path, _short_openalex_id
from daily_agent.connectors.openalex import _abstract_from_inverted_index, _normalize_doi
from daily_agent.models import DigestItem, MaterialRecord

OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def fetch_citation_discovery(
    config: AppConfig,
    target_date: datetime | None = None,
    window_days: int | None = None,
    library: dict[str, MaterialRecord] | None = None,
) -> list[DigestItem]:
    source_config = config.sources.get("citation_discovery", {}) or {}
    if not source_config.get("enabled", False):
        return []
    if not library:
        return []
    target = target_date or datetime.now(timezone.utc)
    days = int(window_days or source_config.get("recent_days", config.sources.get("arxiv", {}).get("recent_days", 7)))
    max_seed_papers = int(source_config.get("max_seed_papers", 10))
    max_results_per_seed = int(source_config.get("max_results_per_seed", 3))
    timeout = float(source_config.get("timeout_seconds", 30))
    if max_seed_papers <= 0 or max_results_per_seed <= 0:
        return []
    seeds = _seed_records(library, max_seed_papers)
    if not seeds:
        return []
    items: list[DigestItem] = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for seed in seeds:
            work_id = _resolve_work_id(client, seed)
            if not work_id:
                continue
            items.extend(_fetch_citing_items(client, seed, work_id, target, days, max_results_per_seed))
    return _dedupe(items)


def _seed_records(library: dict[str, MaterialRecord], max_seed_papers: int) -> list[MaterialRecord]:
    candidates = [
        record
        for record in library.values()
        if record.item_type == "paper" and _openalex_lookup_path(record) and _seedworthy(record)
    ]
    candidates.sort(key=lambda record: (_seed_rank(record), record.score), reverse=True)
    return candidates[:max_seed_papers]


def _seedworthy(record: MaterialRecord) -> bool:
    return bool(record.published_dates or record.quality_status == "published" or record.score > 0)


def _seed_rank(record: MaterialRecord) -> int:
    if record.published_dates or record.quality_status == "published":
        return 3
    if record.score > 0:
        return 1
    return 0


def _resolve_work_id(client: httpx.Client, seed: MaterialRecord) -> str | None:
    lookup_path = _openalex_lookup_path(seed)
    if not lookup_path:
        return None
    if lookup_path.startswith("W"):
        return lookup_path
    try:
        response = client.get(f"{OPENALEX_WORKS_URL}/{lookup_path}")
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return _short_openalex_id(response.json().get("id"))


def _fetch_citing_items(
    client: httpx.Client,
    seed: MaterialRecord,
    work_id: str,
    target: datetime,
    days: int,
    max_results: int,
) -> list[DigestItem]:
    start = (target - timedelta(days=days)).date().isoformat()
    end = (target + timedelta(days=1)).date().isoformat()
    try:
        response = client.get(
            OPENALEX_WORKS_URL,
            params={
                "filter": f"cites:{work_id},from_publication_date:{start},to_publication_date:{end},type:article",
                "sort": "publication_date:desc",
                "per-page": max_results,
            },
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    return [_work_to_discovered_item(work, seed, work_id) for work in response.json().get("results", []) or [] if work.get("title")]


def _work_to_discovered_item(work: dict[str, Any], seed: MaterialRecord, seed_openalex_id: str) -> DigestItem:
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    openalex_url = work.get("id") or ""
    openalex_id = _short_openalex_id(openalex_url) or str(work.get("id") or "")
    doi = _normalize_doi(work.get("doi"))
    landing_url = primary.get("landing_page_url") or openalex_url
    pdf_url = primary.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
    concepts = [str(item.get("display_name")) for item in work.get("concepts", []) or [] if item.get("display_name")]
    raw = {
        "openalex_id": openalex_id,
        "openalex_url": openalex_url,
        "venue": source.get("display_name"),
        "cited_by_count": work.get("cited_by_count"),
        "open_access_url": pdf_url,
        "publication_type": work.get("type"),
        "citation_discovery": {
            "source": "openalex",
            "seed_key": seed.key,
            "seed_title": seed.title,
            "seed_doi": seed.doi,
            "seed_openalex_id": seed_openalex_id,
        },
    }
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
        source_tags=_merge_lists(["citation_discovery", *seed.tags, *concepts]),
        categories=concepts,
        doi=doi,
        score_breakdown={"citation_discovery": 6.0},
        quota_group=seed.quota_group,
        raw=raw,
    )


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


def _merge_lists(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
