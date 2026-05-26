from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem


def fetch_pmlr(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("pmlr", {}) or {}
    if not source_config.get("enabled", False):
        return []
    timeout = float(source_config.get("timeout_seconds", 30))
    max_results = int(source_config.get("max_results_per_volume", source_config.get("max_results_per_query", 25)))
    items: list[DigestItem] = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for volume in source_config.get("volumes", []) or []:
            url = str(volume.get("url") or "")
            if not url:
                continue
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            venue = str(volume.get("venue") or _venue_from_url(url))
            for item in _parse_volume(response.text, url, venue)[:max_results]:
                if _matches_interest(item, config):
                    items.append(item)
    return items


def _parse_volume(html: str, base_url: str, venue: str) -> list[DigestItem]:
    blocks = re.findall(r"<div[^>]*class=[\"'][^\"']*paper[^\"']*[\"'][^>]*>(.*?)</div>", html, flags=re.I | re.S)
    if not blocks:
        blocks = re.findall(r"<p[^>]*class=[\"']title[\"'][^>]*>.*?(?=<p[^>]*class=[\"']title[\"']|$)", html, flags=re.I | re.S)
    return [_block_to_item(block, base_url, venue) for block in blocks if _block_to_item(block, base_url, venue)]


def _block_to_item(block: str, base_url: str, venue: str) -> DigestItem | None:
    title = _class_text(block, "title")
    if not title:
        return None
    authors = _split_authors(_class_text(block, "authors"))
    abstract = _class_text(block, "abstract")
    html_url = _first_href(block, [".html", "/"])
    pdf_url = _first_href(block, [".pdf"])
    landing_url = urljoin(base_url, html_url or "")
    pmlr_id = _pmlr_id(landing_url, title)
    return DigestItem(
        id=pmlr_id,
        source="pmlr",
        item_type="paper",
        title=title,
        url=landing_url,
        pdf_url=urljoin(base_url, pdf_url) if pdf_url else None,
        authors=authors,
        abstract=abstract,
        published_at=_year_from_venue(venue),
        updated_at=_year_from_venue(venue),
        source_tags=["pmlr", venue],
        categories=[venue],
        raw={"pmlr_id": pmlr_id, "pmlr_url": landing_url, "venue": venue},
    )


def _class_text(block: str, class_name: str) -> str | None:
    match = re.search(rf"<[^>]+class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>(.*?)</[^>]+>", block, flags=re.I | re.S)
    return _clean_html(match.group(1)) if match else None


def _first_href(block: str, suffixes: list[str]) -> str | None:
    for href in re.findall(r"href=[\"']([^\"']+)[\"']", block, flags=re.I):
        lowered = href.lower()
        if any(lowered.endswith(suffix) or suffix == "/" for suffix in suffixes):
            return href
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
