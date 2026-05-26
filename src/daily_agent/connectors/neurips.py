from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem

NEURIPS_BASE_URL = "https://papers.nips.cc"


def fetch_neurips(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("neurips", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    years = source_config.get("years") or [target.year - 1, target.year]
    max_results = int(source_config.get("max_results_per_year", source_config.get("max_results_per_query", 25)))
    timeout = float(source_config.get("timeout_seconds", 30))
    items: list[DigestItem] = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for year in years:
            url = f"{NEURIPS_BASE_URL}/paper_files/paper/{int(year)}"
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            for title, paper_url in _paper_links(response.text, url)[:max_results]:
                item = _fetch_detail(client, title, paper_url, int(year))
                if _matches_interest(item, config):
                    items.append(item)
    return items


def _paper_links(html: str, base_url: str) -> list[tuple[str, str]]:
    links = []
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.I | re.S):
        if "paper_files/paper" not in href or "Abstract" not in href:
            continue
        title = _clean_html(label)
        if title:
            links.append((title, urljoin(base_url, href)))
    return links


def _fetch_detail(client: httpx.Client, title: str, url: str, year: int) -> DigestItem:
    abstract = None
    pdf_url = None
    authors: list[str] = []
    try:
        response = client.get(url)
        response.raise_for_status()
        abstract = _abstract_from_detail(response.text)
        pdf_url = _pdf_from_detail(response.text, url)
        authors = _authors_from_detail(response.text)
    except httpx.HTTPError:
        pass
    neurips_id = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
    return DigestItem(
        id=neurips_id,
        source="neurips",
        item_type="paper",
        title=title,
        url=url,
        pdf_url=pdf_url,
        authors=authors,
        abstract=abstract,
        published_at=str(year),
        updated_at=str(year),
        source_tags=["neurips", f"NeurIPS {year}"],
        categories=[f"NeurIPS {year}"],
        raw={"neurips_id": neurips_id, "neurips_url": url, "venue": f"NeurIPS {year}"},
    )


def _abstract_from_detail(html: str) -> str | None:
    match = re.search(r"<h4>\s*Abstract\s*</h4>\s*<p>(.*?)</p>", html, flags=re.I | re.S)
    return _clean_html(match.group(1)) if match else None


def _pdf_from_detail(html: str, base_url: str) -> str | None:
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.I | re.S):
        if href.lower().endswith(".pdf") or "paper" in _clean_html(label).lower():
            return urljoin(base_url, href)
    return None


def _authors_from_detail(html: str) -> list[str]:
    match = re.search(r"<i>(.*?)</i>", html, flags=re.I | re.S)
    if not match:
        return []
    return [part.strip() for part in re.split(r",| and ", _clean_html(match.group(1))) if part.strip()]


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _matches_interest(item: DigestItem, config: AppConfig) -> bool:
    text = " ".join([item.title, item.abstract or "", " ".join(item.categories)]).lower()
    for domain in config.domains:
        if any(keyword.lower() in text for keyword in domain.include_keywords):
            item.quota_group = domain.quota_group
            item.source_tags.append(domain.name)
            return True
    return True
