"""Discover Nature papers by public RSS; verify only public article metadata.

An RSS record, DOI, or successful HTTP response is not publication verification.
Only a matching publisher landing page's citation metadata promotes a candidate.
This collector does not download or claim to have read the article full text.
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from daily_agent.config import AppConfig
from daily_agent.connectors.source_failures import failure_record, finish_collection
from daily_agent.models import DigestItem
from daily_agent.scoring.relevance import passes_topic_gate, topic_relevance_score

DEFAULT_FEEDS = (
    ("Nature", "https://www.nature.com/nature.rss"),
    ("Nature Machine Intelligence", "https://www.nature.com/natmachintell.rss"),
    ("Nature Physics", "https://www.nature.com/nphys.rss"),
    ("Nature Communications", "https://www.nature.com/ncomms.rss"),
    ("Nature Computational Science", "https://www.nature.com/natcomputsci.rss"),
    ("Nature Electronics", "https://www.nature.com/natelectron.rss"),
    ("Nature Reviews Physics", "https://www.nature.com/natrevphys.rss"),
    ("npj Quantum Information", "https://www.nature.com/npjqi.rss"),
)
_VENUES = {name.casefold(): name for name, _ in DEFAULT_FEEDS}
_FEED_NAMES = {url: name for name, url in DEFAULT_FEEDS}
_NON_PAPERS = {
    "editorial", "comment", "commentary", "news", "newsandviews", "newsfeature",
    "researchhighlight", "researchbriefing", "correction", "authorcorrection",
    "publishercorrection", "retraction", "obituary", "bookreview", "correspondence",
}


class NatureVerificationError(ValueError):
    """Public landing metadata did not prove this candidate's publication."""


def fetch_nature(config: AppConfig, target_date: datetime | None = None,
                 window_days: int | None = None) -> list[DigestItem]:
    settings = config.sources.get("nature", {}) or {}
    if not settings.get("enabled", False):
        return []
    target = target_date or datetime.now(timezone.utc)
    days = max(0, int(window_days if window_days is not None else settings.get("recent_days", 30)))
    timeout = min(60.0, max(0.1, float(settings.get("timeout_seconds", 15))))
    per_feed = min(100, max(0, int(settings.get("max_articles_per_feed", 20))))
    detail_limit = min(200, max(0, int(settings.get("max_detail_pages", 24))))
    keep_unverified = bool(settings.get("include_unverified", True))
    feeds = settings.get("feeds", [{"name": name, "url": url} for name, url in DEFAULT_FEEDS]) or []
    candidates: list[DigestItem] = []
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    seen: set[str] = set()

    # Do not follow an off-site identity/login redirect or retry an access denial.
    with httpx.Client(timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for feed in feeds[:16]:
            url = str(feed.get("url") or "") if isinstance(feed, dict) else str(feed)
            venue = str(feed.get("name") or feed.get("venue") or "") if isinstance(feed, dict) else ""
            venue = venue or _FEED_NAMES.get(url, "")
            try:
                _validate_feed_url(url)
                response = _get_public(client, url, article=False)
                parsed = _parse_feed(response.text, url, venue)
                successful_requests += 1
            except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                errors.append(exc)
                failures.append(failure_record(url, exc))
                continue
            relevant = [item for item in parsed if _within_window(item.published_at, target, days)
                        and passes_topic_gate(item, config)]
            relevant.sort(key=lambda item: topic_relevance_score(item, config), reverse=True)
            for item in relevant[:per_feed]:
                if item.url in seen:
                    continue
                seen.add(item.url)
                candidates.append(item)

        candidates.sort(key=lambda item: topic_relevance_score(item, config), reverse=True)
        for position, item in enumerate(candidates):
            if position >= detail_limit:
                _mark_unverified(item, "detail_page_budget_exhausted")
                if keep_unverified:
                    items.append(item)
                continue
            try:
                response = _get_public(client, item.url, article=True)
                verified = _verify_landing(item, response.text, str(response.url))
                successful_requests += 1
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failure = failure_record(item.url, exc)
                failures.append(failure)
                _mark_unverified(item, str(exc))
                item.raw["primary_verification_failure"] = failure
                if keep_unverified:
                    items.append(item)
                continue
            # A public news/editorial/correction page is not a research paper.
            if verified is not None and _within_window(verified.published_at, target, days):
                items.append(verified)
    return finish_collection("Nature", items, failures, errors, successful_requests)


def _validate_host(url: str) -> Any:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.nature.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise NatureVerificationError("Expected an HTTPS www.nature.com public URL")
    return parsed


def _canonical_article_url(url: str) -> str:
    parsed = _validate_host(url)
    if (not re.fullmatch(r"/articles/[A-Za-z0-9][A-Za-z0-9.-]*", parsed.path)
            or parsed.path.lower().endswith((".pdf", ".xml", ".epub"))):
        raise NatureVerificationError("Expected a canonical Nature article landing URL")
    return urlunsplit(("https", "www.nature.com", parsed.path, "", ""))


def _validate_feed_url(url: str) -> None:
    parsed = _validate_host(url)
    if not re.fullmatch(r"/[a-z][a-z0-9-]*\.rss", parsed.path) or parsed.query or parsed.fragment:
        raise NatureVerificationError("Expected an official Nature journal RSS URL")


def _get_public(client: httpx.Client, url: str, *, article: bool) -> httpx.Response:
    expected = _canonical_article_url(url) if article else url
    for _ in range(4):
        if article:
            if _canonical_article_url(url) != expected:
                raise NatureVerificationError("Article redirect changed the canonical article")
        else:
            _validate_feed_url(url)
        response = client.get(url)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                response.raise_for_status()
            url = urljoin(url, location or "")
            continue
        response.raise_for_status()
        if article and _canonical_article_url(str(response.url)) != expected:
            raise NatureVerificationError("Response URL does not match the requested article")
        if not article:
            _validate_feed_url(str(response.url))
        if len(response.content) > 4_000_000:
            raise NatureVerificationError("Public metadata response exceeds 4 MB limit")
        return response
    raise NatureVerificationError("Too many public Nature redirects")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _texts(element: ET.Element, name: str) -> list[str]:
    return [_clean("".join(child.itertext())) for child in element if _local(child.tag) == name]


def _text(element: ET.Element, *names: str) -> str:
    for name in names:
        values = _texts(element, name)
        if values and values[0]:
            return values[0]
    return ""


def _parse_feed(xml: str, feed_url: str, expected_venue: str) -> list[DigestItem]:
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise NatureVerificationError("RSS must not contain document type or entity declarations")
    root = ET.fromstring(xml)
    if _local(root.tag) not in {"rss", "rdf", "feed"}:
        raise NatureVerificationError("Nature RSS response is not a feed")
    channel = next((node for node in root if _local(node.tag) == "channel"), root)
    channel_venue = _text(channel, "publicationname", "title")
    venue = expected_venue or channel_venue
    if venue.casefold() not in _VENUES:
        raise NatureVerificationError("RSS journal is outside the configured Nature journal family")
    if channel_venue and _normalize(channel_venue) != _normalize(venue):
        raise NatureVerificationError("RSS channel title does not match the configured journal")
    result = []
    for entry in root.iter():
        if _local(entry.tag) not in {"item", "entry"}:
            continue
        title = _text(entry, "title")
        url = _text(entry, "link", "url")
        if not url:
            url = next((child.get("href", "") for child in entry
                        if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate"), "")
        if not title or not url:
            continue
        # Invalid candidates are never fetched or allowed to impersonate this source.
        url = _canonical_article_url(url)
        item_venue = _text(entry, "publicationname") or venue
        if _normalize(item_venue) != _normalize(venue):
            raise NatureVerificationError("RSS article journal does not match its feed")
        article_type = _text(entry, "type", "aggregationtype")
        if _normalize_type(article_type) in _NON_PAPERS:
            continue
        published = _date(_text(entry, "date", "publicationdate", "pubdate", "published"))
        doi = _doi(_text(entry, "doi", "identifier"))
        summary = _text(entry, "description", "summary", "encoded") or None
        article_id = url.rsplit("/", 1)[-1]
        item = DigestItem(
            id=article_id, source="nature", item_type="paper", title=title, url=url,
            authors=_texts(entry, "creator"), abstract=summary, doi=doi,
            published_at=published, updated_at=published,
            source_tags=["nature", venue], categories=[],
            raw={"nature_id": article_id, "nature_url": url, "venue": venue,
                 "publication_type": "journal-article", "discovery_source": "publisher_rss",
                 "rss_url": feed_url, "rss_metadata": {"title": title, "url": url,
                 "venue": venue, "doi": doi, "published_at": published},
                 "abstract_source": "publisher_rss" if summary else None},
        )
        _mark_unverified(item, "landing_not_checked")
        result.append(item)
    return result


class _LandingMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.canonicals: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            content = _clean(values.get("content", ""))
            if name and content:
                self.meta.setdefault(name, []).append(content)
        if tag.casefold() == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonicals.append(values.get("href", ""))

    def one(self, *names: str) -> str:
        for name in names:
            values = self.meta.get(name, [])
            if values:
                if len({_normalize(value) for value in values}) != 1:
                    raise NatureVerificationError(f"Conflicting landing metadata: {name}")
                return values[0]
        return ""


def _verify_landing(item: DigestItem, html: str, response_url: str) -> DigestItem | None:
    expected_url = _canonical_article_url(item.url)
    if _canonical_article_url(response_url) != expected_url:
        raise NatureVerificationError("Landing response changed the canonical article")
    page = _LandingMetadata()
    page.feed(html)
    for url in [*page.canonicals, *page.meta.get("citation_fulltext_html_url", []),
                *page.meta.get("citation_abstract_html_url", [])]:
        if _canonical_article_url(urljoin(response_url, url)) != expected_url:
            raise NatureVerificationError("Landing canonical metadata does not match the RSS article")
    title = page.one("citation_title")
    venue = page.one("citation_journal_title")
    if not title or not venue:
        raise NatureVerificationError("Landing page lacks citation_title or citation_journal_title")
    if _normalize(title) in {"", "untitled"} or _normalize(title) != _normalize(item.title):
        raise NatureVerificationError("Landing citation title does not match the RSS candidate")
    if venue.casefold() not in _VENUES or _normalize(venue) != _normalize(item.raw.get("venue")):
        raise NatureVerificationError("Landing citation journal does not match the RSS journal")
    doi_value = page.one("citation_doi")
    doi = _doi(doi_value)
    if doi_value and not doi:
        raise NatureVerificationError("Landing citation DOI is malformed")
    if doi and item.doi and doi.casefold() != item.doi.casefold():
        raise NatureVerificationError("Landing citation DOI does not match the RSS candidate")
    published_value = page.one("citation_online_date", "citation_publication_date", "citation_date")
    published = _date(published_value)
    if published_value and not published:
        raise NatureVerificationError("Landing citation publication date is malformed")
    article_type = page.one("citation_article_type", "dc.type", "dc.type.article", "article_type")
    if _normalize_type(article_type) in _NON_PAPERS:
        return None
    item.title, item.url, item.doi = title, expected_url, doi
    item.published_at = published
    item.updated_at = published
    item.authors = page.meta.get("citation_author", []) or item.authors
    abstract = page.one("citation_abstract")
    if abstract:
        item.abstract = abstract
        item.raw["abstract_source"] = "publisher_landing_citation_metadata"
    item.raw.update({
        "nature_url": expected_url, "venue": venue, "publication_status": "published",
        "primary_landing_verified": True,
        "publication_type": "review" if "review" in article_type.casefold() else "journal-article",
        "publisher_article_type": article_type or None,
        "primary_verification": {
            "verified": True, "method": "publisher_landing_citation_metadata",
            "url": expected_url, "title": title, "venue": venue,
            "doi": doi, "published_at": published,
        },
    })
    return item


def _mark_unverified(item: DigestItem, reason: str) -> None:
    item.raw.update({"publication_status": "metadata_only", "primary_landing_verified": False,
                     "primary_verification": {"verified": False, "reason": reason,
                     "url": item.url, "method": "publisher_landing_citation_metadata"}})


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _normalize(value: Any) -> str:
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _normalize_type(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.casefold().replace("&", "and"))


def _doi(value: str) -> str | None:
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def _date(value: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("/", "-").replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (ValueError, TypeError, OverflowError):
            return None


def _within_window(published: str | None, target: datetime, days: int) -> bool:
    # Unknown dates remain discovery leads; they never prove a publication date.
    return published is None or (target.date() - timedelta(days=days)).isoformat() <= published <= target.date().isoformat()
