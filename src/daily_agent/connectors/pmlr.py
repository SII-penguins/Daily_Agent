from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection


def fetch_pmlr(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("pmlr", {}) or {}
    if not source_config.get("enabled", False):
        return []
    timeout = float(source_config.get("timeout_seconds", 30))
    max_results = int(source_config.get("max_results_per_volume", source_config.get("max_results_per_query", 25)))
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for volume in source_config.get("volumes", []) or []:
            url = str(volume.get("url") or "")
            if not url:
                continue
            try:
                response = client.get(url)
                response.raise_for_status()
                successful_requests += 1
            except httpx.HTTPError as exc:
                errors.append(exc)
                failures.append(failure_record(url, exc))
                continue
            venue = str(volume.get("venue") or _venue_from_url(url))
            candidates = _parse_volume(response.text, url, venue)
            if source_config.get("prioritize_relevant", False):
                from daily_agent.scoring.relevance import topic_relevance_score
                candidates.sort(key=lambda item: topic_relevance_score(item, config), reverse=True)
            for item in candidates[:max_results]:
                if _matches_interest(item, config):
                    items.append(item)
    return finish_collection("PMLR", items, failures, errors, successful_requests)


def _parse_volume(html: str, base_url: str, venue: str) -> list[DigestItem]:
    blocks = re.findall(r"<div[^>]*class=[\"'][^\"']*paper[^\"']*[\"'][^>]*>(.*?)</div>", html, flags=re.I | re.S)
    if not blocks:
        blocks = re.findall(r"<p[^>]*class=[\"']title[\"'][^>]*>.*?(?=<p[^>]*class=[\"']title[\"']|$)", html, flags=re.I | re.S)
    publication = _volume_publication_info(html, base_url, venue)
    items = []
    for block in blocks:
        item = _block_to_item(block, base_url, venue, publication)
        if item is not None:
            items.append(item)
    return items


def _block_to_item(block: str, base_url: str, venue: str, publication: dict[str, Any] | None = None) -> DigestItem | None:
    title = _class_text(block, "title")
    if not title:
        return None
    authors = _split_authors(_class_text(block, "authors"))
    abstract = _class_text(block, "abstract")
    html_url = _first_href(block, [".html", "/"])
    pdf_url = _first_href(block, [".pdf"])
    landing_url = urljoin(base_url, html_url or "")
    pmlr_id = _pmlr_id(landing_url, title)
    publication = publication or _volume_publication_info("", base_url, venue)
    return DigestItem(
        id=pmlr_id,
        source="pmlr",
        item_type="paper",
        title=title,
        url=landing_url,
        pdf_url=urljoin(base_url, pdf_url) if pdf_url else None,
        authors=authors,
        abstract=abstract,
        published_at=publication.get("publication_date"),
        source_tags=["pmlr", venue],
        categories=[venue],
        raw={"pmlr_id": pmlr_id, "pmlr_url": landing_url, "venue": venue, **publication},
    )


def _volume_publication_info(html: str, base_url: str, venue: str) -> dict[str, Any]:
    """Read the volume's release date, never its conference or build date.

    PMLR's index description explicitly distinguishes "Held ... on" from
    "Published as Volume ... on". Citation month fields describe the event and
    are deliberately not treated as publication dates.
    """
    header = re.split(r"<div[^>]*class=[\"'][^\"']*\bpaper\b", html, maxsplit=1, flags=re.I)[0]
    # Keep metadata attribute values as well as visible title/header text.
    description = unescape(re.sub(r"\s+", " ", header))
    match = re.search(
        r"Published\s+as\s+Volume\s+\d+\s+by\s+[^<>\"]{0,250}?\s+on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        description, flags=re.I,
    )
    publication_date = None
    if match:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                publication_date = datetime.strptime(match.group(1), fmt).date().isoformat()
                break
            except ValueError:
                continue
    year = _year_from_venue(venue)
    result: dict[str, Any] = {
        "publication_date": publication_date or year,
        "date_precision": "day" if publication_date else "year" if year else "unknown",
        "publication_date_precision": "day" if publication_date else "year" if year else "unknown",
        "publication_date_basis": "official_volume_publication" if publication_date else "venue_year" if year else "unknown",
        "publication_date_source_url": base_url,
        "conference_year": year,
    }
    event = re.search(r"Held\s+[^<>\"]{0,250}?\s+on\s+(.{1,80}?)\s+Published\s+as\s+Volume", description, flags=re.I)
    if event:
        result["conference_dates"] = event.group(1).strip()
    if publication_date and match:
        result["publication_date_evidence"] = match.group(0)
    return result


def _class_text(block: str, class_name: str) -> str | None:
    match = re.search(rf"<[^>]+class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>(.*?)</[^>]+>", block, flags=re.I | re.S)
    return _clean_html(match.group(1)) if match else None


def _first_href(block: str, suffixes: list[str]) -> str | None:
    hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", block, flags=re.I)
    for suffix in suffixes:
        for href in hrefs:
            if href.lower().endswith(suffix):
                return unescape(href)
    return None


def _split_authors(value: str | None) -> list[str]:
    return [part.strip() for part in re.split(r",| and ", value or "") if part.strip()]


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _pmlr_id(url: str, title: str) -> str:
    if url:
        return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _venue_from_url(url: str) -> str:
    return f"PMLR {url.rstrip('/').rsplit('/', 1)[-1]}"


def _year_from_venue(venue: str) -> str | None:
    match = re.search(r"(20\d{2})", venue)
    return match.group(1) if match else None


def _matches_interest(item: DigestItem, config: AppConfig) -> bool:
    text = " ".join([item.title, item.abstract or "", " ".join(item.categories)]).lower()
    for domain in config.domains:
        if any(keyword.lower() in text for keyword in domain.include_keywords):
            item.quota_group = domain.quota_group
            item.source_tags.append(domain.name)
            return True
    return True
