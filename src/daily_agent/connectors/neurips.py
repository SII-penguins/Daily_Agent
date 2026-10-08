from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection

NEURIPS_BASE_URL = "https://papers.nips.cc"


def fetch_neurips(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("neurips", {}) or {}
    if not source_config.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    years = source_config.get("years") or [target.year - 1, target.year]
    max_results = int(source_config.get("max_results_per_year", source_config.get("max_results_per_query", 25)))
    max_detail_pages = int(source_config.get("max_detail_pages_per_year", max_results) or 0)
    max_index_pages = max(1, min(int(source_config.get("max_index_pages_per_year", 4)), 16))
    timeout = float(source_config.get("timeout_seconds", 30))
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for year in years:
            url = f"{NEURIPS_BASE_URL}/paper_files/paper/{int(year)}"
            links, index_successes = _collect_year_links(
                client, url, int(year), max_index_pages, failures, errors,
            )
            successful_requests += index_successes
            allowed_tracks = source_config.get("allowed_tracks")
            if allowed_tracks:
                links = [(title, link) for title, link in links if _track(link) in allowed_tracks or _track(link) == "legacy"]
            if source_config.get("prioritize_relevant", False):
                from daily_agent.scoring.relevance import topic_relevance_score
                links.sort(key=lambda row: topic_relevance_score(_paper_stub(row[0], row[1], int(year)), config), reverse=True)
            for index, (title, paper_url) in enumerate(links[:max_results]):
                if max_detail_pages <= 0 or index < max_detail_pages:
                    item = _fetch_detail(client, title, paper_url, int(year), failures=failures, errors=errors)
                else:
                    item = _paper_stub(title, paper_url, int(year))
                if _matches_interest(item, config):
                    items.append(item)
    return finish_collection("NeurIPS", items, failures, errors, successful_requests)


def _collect_year_links(client, initial_url, year, max_index_pages, failures, errors):
    """Follow published same-year volume links, including a page's "See also".

    In 2025 the year endpoint serves Creative AI, while the main conference is
    a linked volume. A page containing papers may still need traversal.
    """
    pending = [initial_url]
    visited = set()
    links = {}
    successful_requests = 0
    index_requests = 0
    while pending and index_requests < max_index_pages:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        index_requests += 1
        try:
            response = client.get(url)
            response.raise_for_status()
            successful_requests += 1
        except httpx.HTTPError as exc:
            errors.append(exc)
            failures.append(failure_record(url, exc))
            continue
        base_url = str(response.url)
        visited.add(base_url)
        for title, paper_url in _paper_links(response.text, base_url):
            if f"/paper/{year}/" in urlsplit(paper_url).path:
                links.setdefault(paper_url, title)
        for linked_url in _index_links(response.text, base_url, year):
            if linked_url not in visited and linked_url not in pending:
                pending.append(linked_url)
    return [(title, url) for url, title in links.items()], successful_requests


def _index_links(html: str, base_url: str, year: int) -> list[str]:
    links = []
    for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", html, flags=re.I):
        url = urljoin(base_url, unescape(href))
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"papers.nips.cc", "proceedings.neurips.cc"}:
            continue
        if parsed.query or parsed.fragment:
            continue
        if re.fullmatch(rf"/paper_files/paper/{year}(?:/vol[\w-]+)?/?", parsed.path):
            if url not in links:
                links.append(url)
    return links


def _paper_links(html: str, base_url: str) -> list[tuple[str, str]]:
    links = []
    for href, label in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.I | re.S):
        if "paper_files/paper" not in href or "Abstract" not in href:
            continue
        title = _clean_html(label)
        if title:
            links.append((title, urljoin(base_url, href)))
    return links


def _fetch_detail(client: httpx.Client, title: str, url: str, year: int, *, failures=None, errors=None) -> DigestItem:
    abstract = None
    pdf_url = None
    authors: list[str] = []
    publication = _publication_info("", year, url)
    try:
        response = client.get(url)
        response.raise_for_status()
        abstract = _abstract_from_detail(response.text)
        pdf_url = _pdf_from_detail(response.text, url)
        authors = _authors_from_detail(response.text)
        publication = _publication_info(response.text, year, url)
    except httpx.HTTPError as exc:
        if failures is None or errors is None:
            raise
        failures.append(failure_record(url, exc))
        errors.append(exc)
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
        published_at=publication["publication_date"],
        source_tags=["neurips", f"NeurIPS {year}"],
        categories=[f"NeurIPS {year}"],
        raw={"neurips_id": neurips_id, "neurips_url": url, "venue": f"NeurIPS {year}", "proceedings_track": _track(url), **publication},
    )


def _paper_stub(title: str, url: str, year: int) -> DigestItem:
    neurips_id = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html")
    return DigestItem(
        id=neurips_id,
        source="neurips",
        item_type="paper",
        title=title,
        url=url,
        published_at=str(year),
        source_tags=["neurips", f"NeurIPS {year}"],
        categories=[f"NeurIPS {year}"],
        raw={"neurips_id": neurips_id, "neurips_url": url, "venue": f"NeurIPS {year}", "proceedings_track": _track(url), "detail_skipped": True, **_publication_info("", year, url)},
    )


class _PublicationMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "meta":
            attributes = dict(attrs)
            name = str(attributes.get("name") or attributes.get("property") or "").lower()
            self.values[name] = attributes.get("content") or ""


def _publication_info(html: str, year: int, url: str) -> dict:
    parser = _PublicationMetadata()
    parser.feed(html)
    publication_date, precision, basis = str(year), "year", "proceedings_year"
    for key in ("citation_publication_date", "dc.date.issued", "dcterms.issued", "article:published_time"):
        value = parser.values.get(key, "").strip()
        if not value:
            continue
        # Do not promote a year/month-only citation to an invented day.
        for fmt, width, resolved_precision in (("%Y/%m/%d", 10, "day"), ("%Y-%m-%d", 10, "day"), ("%Y/%m", 7, "month"), ("%Y-%m", 7, "month"), ("%Y", 4, "year")):
            if len(value) != width and not (width == 10 and re.match(r"^\d{4}-\d{2}-\d{2}T", value)):
                continue
            try:
                parsed = datetime.strptime(value[:width], fmt)
            except ValueError:
                continue
            publication_date = parsed.strftime({"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[resolved_precision])
            precision, basis = resolved_precision, key
            break
        if basis == key:
            break
    return {
        "publication_date": publication_date,
        "date_precision": precision,
        "publication_date_precision": precision,
        "publication_date_basis": basis,
        "publication_date_source_url": url,
        "conference_year": str(year),
    }


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


def _track(url):
    match = re.search(r'-Abstract-(.+)\.html$', url)
    return match.group(1) if match else 'legacy'
