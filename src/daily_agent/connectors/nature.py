"""Discover Nature leads by rolling RSS and supplied Crossref journal metadata.

An RSS record, DOI, or successful HTTP response is not publication verification.
Only a matching publisher landing page's citation metadata promotes a candidate.
This collector does not download or claim to have read the article full text.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
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
from daily_agent.connectors.official_metadata import (MetadataBudget, MetadataCache, MetadataLimit,
    discovery_bounds, discovery_relevant, exact_date_in_window, fair_candidate_order,
    failure_status, fetch_text)
from daily_agent.models import DigestItem
from daily_agent.scoring.relevance import passes_topic_gate

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
                 window_days: int | None = None, *, discovery_seeds: list[DigestItem] | None = None,
                 coverage: dict | None = None, budget: MetadataBudget | None = None,
                 cache: MetadataCache | None = None) -> list[DigestItem]:
    settings = config.sources.get("nature", {}) or {}
    report = coverage if coverage is not None else {}
    report.update(complete=False, archive_status="archive_not_validated", discovery="rolling_rss_and_supplied_journal_leads",
                  candidates=0, pending=0, cache_hits=0, requests=0)
    if not settings.get("enabled", False):
        report["reason"] = "disabled"
        return []
    target = target_date or datetime.now(timezone.utc)
    days = max(0, int(window_days if window_days is not None else settings.get("recent_days", 30)))
    bounds = discovery_bounds(config, target, days)
    timeout = min(60.0, max(0.1, float(settings.get("timeout_seconds", 15))))
    per_feed = min(100, max(0, int(settings.get("max_articles_per_feed", 20))))
    detail_limit = min(24, max(0, int(settings.get("max_detail_pages", 24))))
    keep_unverified = bool(settings.get("include_unverified", True))
    feeds = settings.get("feeds", [{"name": name, "url": url} for name, url in DEFAULT_FEEDS]) or []
    budget = budget or MetadataBudget(max_requests=min(48, max(0, int(settings.get("max_metadata_requests", 48)))),
                                      max_bytes=24_000_000, seconds=min(150.0, max(0.0, float(settings.get("metadata_budget_seconds", 150)))))
    initial_requests = budget.requests
    cache = cache or MetadataCache(config, "nature")
    candidates, items, failures, errors = [], [], [], []
    successful_requests = 0
    host_blocked = False
    seen: set[str] = set()
    discovery_pool: dict[str, DigestItem] = {}

    def add(candidate):
        if candidate.url not in seen and (candidate.published_at is None or exact_date_in_window(candidate.published_at, bounds)):
            if exact_date_in_window(candidate.published_at, bounds):
                discovery_pool[candidate.url] = candidate
            # These broad title cues only earn a bounded landing lookup.
            if discovery_relevant(candidate, config):
                seen.add(candidate.url)
                candidates.append(candidate)

    pool_candidates = []
    pool = cache.get("https://www.nature.com/", kind="discovery")
    if pool and pool["status"] == "index":
        for row in pool["payload"].get("items", [])[:10000]:
            candidate = DigestItem.from_dict(row)
            if exact_date_in_window(candidate.published_at, bounds):
                pool_candidates.append(candidate)
        report["discovery_cache_checked_at"] = pool["checked_at"]
    for seed in discovery_seeds or []:
        candidate = _nature_seed_from_crossref(seed)
        if candidate is not None:
            add(candidate)
    with httpx.Client(timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for feed in feeds[:8]:
            url = str(feed.get("url") or "") if isinstance(feed, dict) else str(feed)
            venue = str(feed.get("name") or feed.get("venue") or "") if isinstance(feed, dict) else ""
            venue = venue or _FEED_NAMES.get(url, "")
            try:
                _validate_feed_url(url)
                cached = cache.get(url, kind="rss")
                if cached and cached["status"] == "index":
                    parsed = [DigestItem.from_dict(row) for row in cached["payload"]["items"]]
                    report["cache_hits"] += 1
                elif cached:
                    failure = cached["payload"].get("failure") or {"target": url, "type": "CachedMetadataFailure",
                        "status_code": None, "message": cached["payload"].get("message", cached["status"])}
                    failures.append({**failure, "checked_at": cached["checked_at"], "cached": True})
                    errors.append(NatureVerificationError(f"Cached {cached['status']}: {failure['message']}"))
                    if cached["status"] in {"blocked", "rate_limited"}:
                        host_blocked = True
                    continue
                elif host_blocked:
                    continue
                else:
                    text, _, _ = _get_public(client, url, article=False, budget=budget)
                    parsed = _parse_feed(text, url, venue)
                    cache.put(url, {"items": [item.to_dict() for item in parsed]}, kind="rss", status="index")
                successful_requests += 1
            except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                errors.append(exc)
                failures.append(failure_record(url, exc))
                if not str(exc).startswith("Cached "):
                    cache.put(url, {"message": str(exc), "failure": failure_record(url, exc)},
                              kind="rss", status=failure_status(exc), ttl_seconds=_retry_after_ttl(exc))
                if failure_status(exc) in {"blocked", "rate_limited"} or str(exc).startswith(("Cached blocked:", "Cached rate_limited:")):
                    host_blocked = True
                continue
            for item in parsed:
                add(item)

        # Fresh discovery identity always outranks older persisted title stubs.
        for candidate in pool_candidates:
            add(candidate)
        report["candidates"] = len(candidates)
        candidates = fair_candidate_order(candidates, config)
        attempted_details = 0
        for item in candidates:
            try:
                cached = cache.get(item.url)
                if cached and cached["status"] == "nonresearch_item":
                    prior = cached["payload"]
                    if prior.get("input_title") != item.title or prior.get("doi") != item.doi:
                        cached = None
                    else:
                        report["nonresearch_cached"] = report.get("nonresearch_cached", 0) + 1
                        report["cache_hits"] += 1
                        successful_requests += 1
                        discovery_pool.pop(item.url, None)
                        continue
                if cached and cached["status"] == "verified":
                    try:
                        _check_cached_identity(item, DigestItem.from_dict(cached["payload"]["item"]))
                    except NatureVerificationError:
                        # A fresh title/DOI binding must be checked on the official page.
                        report["cache_identity_invalidated"] = report.get("cache_identity_invalidated", 0) + 1
                        cached = None
                if cached and cached["status"] == "verified":
                    verified = DigestItem.from_dict(cached["payload"]["item"])
                    verified.raw["metadata_cache_checked_at"] = cached["checked_at"]
                    verified.raw["metadata_cache_hit"] = True
                    report["cache_hits"] += 1
                    successful_requests += 1
                elif cached:
                    failure = cached["payload"].get("failure") or {"target": item.url, "type": "CachedMetadataFailure",
                        "status_code": None, "message": cached["payload"].get("message", cached["status"])}
                    _mark_unverified(item, f"cached_{cached['status']}")
                    item.raw.update(primary_verification_failure=failure,
                                    metadata_cache_checked_at=cached["checked_at"])
                    failures.append(failure)
                    errors.append(NatureVerificationError(f"Cached {cached['status']}: {failure['message']}"))
                    if cached["status"] in {"blocked", "rate_limited"}:
                        host_blocked = True
                    if keep_unverified:
                        items.append(item)
                    continue
                elif attempted_details >= detail_limit or host_blocked:
                    _mark_unverified(item, "host_access_denied_or_rate_limited" if host_blocked else "detail_page_budget_exhausted")
                    report["pending"] += 1
                    if keep_unverified:
                        items.append(item)
                    continue
                else:
                    attempted_details += 1
                    text, final_url, response_hash = _get_public(client, item.url, article=True, budget=budget)
                    verified = _verify_landing(item, text, final_url)
                    successful_requests += 1
                    if verified is not None:
                        verified.raw["primary_metadata_response_sha256"] = response_hash
                        verified.raw["metadata_evidence"] = {
                            "source_url": final_url, "response_sha256": response_hash,
                            "kind": "publisher_metadata_excerpt",
                            "citation_html": verified.raw["primary_metadata_evidence_regions"],
                            "citation_excerpt": "\n".join(verified.raw["primary_metadata_evidence_regions"]),
                            "abstract_html": verified.raw.get("abstract_evidence_region"),
                        }
                        cache.put(item.url, {"item": verified.to_dict()}, status="verified")
                    else:
                        # A verified editorial/correction is an explicit negative
                        # content result, not a transport failure or another paper.
                        cache.put(item.url, {"input_title": item.title, "doi": item.doi,
                            "publisher_article_type": item.raw.get("publisher_article_type"),
                            "source_url": final_url, "response_sha256": response_hash},
                            status="nonresearch_item", ttl_seconds=7 * 86400)
                        discovery_pool.pop(item.url, None)
                        report["nonresearch_excluded"] = report.get("nonresearch_excluded", 0) + 1
                if verified is not None and exact_date_in_window(verified.published_at, bounds):
                    discovery_pool[verified.url] = verified
                    if passes_topic_gate(verified, config):
                        items.append(verified)
                elif verified is not None:
                    discovery_pool.pop(verified.url, None)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failure = failure_record(item.url, exc)
                failures.append(failure)
                _mark_unverified(item, str(exc))
                item.raw["primary_verification_failure"] = failure
                status = failure_status(exc)
                cache.put(item.url, {"message": str(exc), "failure": failure}, status=status, ttl_seconds=_retry_after_ttl(exc))
                if status in {"blocked", "rate_limited"}:
                    host_blocked = True
                if status == "budget_exhausted":
                    report["pending"] += 1
                if keep_unverified:
                    items.append(item)
    pool_rows = sorted(discovery_pool.values(), key=lambda item: (item.published_at or "", item.url), reverse=True)
    report["discovery_pool_truncated"] = len(pool_rows) > 10000
    cache.put("https://www.nature.com/", {"items": [_discovery_stub(item) for item in pool_rows[:10000]]},
              kind="discovery", status="index", ttl_seconds=120 * 86400)
    report.update(requests=budget.requests - initial_requests, shared_budget_requests=budget.requests,
                  failures=failures, host_blocked=host_blocked, discovery_pool_size=min(len(pool_rows), 10000))
    # The old per-feed cap is now applied after all bounded enrichment attempts.
    counts: dict[str, int] = {}
    final = []
    for item in items:
        venue = str(item.raw.get("venue") or "")
        if counts.get(venue, 0) < per_feed:
            final.append(item)
            counts[venue] = counts.get(venue, 0) + 1
    return finish_collection("Nature", final, failures, errors, successful_requests)


def _discovery_stub(item: DigestItem) -> dict:
    raw = item.raw
    stub = DigestItem(id=item.id, source="nature", item_type="paper", title=item.title,
        url=item.url, doi=item.doi, published_at=item.published_at, source_tags=list(item.source_tags),
        raw={key: raw[key] for key in ("nature_id", "nature_url", "venue", "publication_type",
            "discovery_source", "discovery_partial_recall", "rss_url") if key in raw})
    _mark_unverified(stub, "landing_not_checked")
    return stub.to_dict()


def _retry_after_ttl(exc) -> float | None:
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 429:
        return None
    value = response.headers.get("retry-after", "")
    try:
        return max(6 * 3600, float(value))
    except ValueError:
        try:
            retry = parsedate_to_datetime(value)
            if retry.tzinfo is None:
                retry = retry.replace(tzinfo=timezone.utc)
            return max(6 * 3600, (retry - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 6 * 3600


def _nature_seed_from_crossref(item: DigestItem) -> DigestItem | None:
    """Create only an unverified lead; never follow arbitrary DOI redirects."""
    if item.source != "crossref" or item.raw.get("publication_type") != "journal-article":
        return None
    venue = _VENUES.get(str(item.raw.get("venue") or "").casefold())
    doi = _doi(str(item.doi or ""))
    if not venue or not doi or not re.fullmatch(r"10\.1038/s\d{5}-\d{3}-\d{5}-[a-z0-9]", doi, re.I):
        return None
    expected_id = doi.split("/", 1)[1].lower()
    url = None
    for supplied in (item.raw.get("discovery_primary_url"), item.url):
        if not supplied:
            continue
        try:
            candidate_url = _canonical_article_url(str(supplied))
        except ValueError:
            continue
        if candidate_url.rsplit("/", 1)[-1].lower() != expected_id:
            return None
        url = candidate_url
        break
    url = url or f"https://www.nature.com/articles/{expected_id}"
    # A registry date is only a discovery hint, never primary date evidence.
    candidate = DigestItem(id=expected_id, source="nature", item_type="paper", title=item.title,
        url=url, authors=list(item.authors), abstract=item.abstract, doi=doi,
        published_at=item.published_at if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.published_at or "") else None,
        source_tags=["nature", venue], raw={"nature_id": expected_id, "nature_url": url,
        "venue": venue, "publication_type": "journal-article", "discovery_source": "crossref_preferred_journal",
        "discovery_partial_recall": True, "abstract_source": "crossref_metadata" if item.abstract else None,
        "crossref_metadata": {"title": item.title, "doi": doi, "venue": venue,
                              "published_at": item.published_at, "url": item.url}})
    _mark_unverified(candidate, "landing_not_checked")
    return candidate


def _check_cached_identity(candidate: DigestItem, verified: DigestItem) -> None:
    if (candidate.url != verified.url or _normalize(candidate.title) != _normalize(verified.title)
            or _normalize(candidate.raw.get("venue")) != _normalize(verified.raw.get("venue"))
            or candidate.doi and str(candidate.doi).casefold() != str(verified.doi).casefold()
            or not verified.raw.get("primary_landing_verified")):
        raise NatureVerificationError("Cached landing identity does not match this discovery candidate")


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


def _get_public(client: httpx.Client, url: str, *, article: bool,
                budget: MetadataBudget | None = None) -> tuple[str, str, str]:
    expected = _canonical_article_url(url) if article else url
    def validate(value):
        if article:
            if _canonical_article_url(value) != expected:
                raise NatureVerificationError("Response URL does not match the requested article")
        else:
            _validate_feed_url(value)
    return fetch_text(client, url, budget or MetadataBudget(max_requests=1), 4_000_000, validate,
        expected_types=("text/html", "application/xhtml+xml") if article else
        ("application/rss+xml", "application/rdf+xml", "application/xml", "text/xml"))


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
                 "abstract_source": "publisher_rss_summary" if summary else None},
        )
        _mark_unverified(item, "landing_not_checked")
        result.append(item)
    return result


class _LandingMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.canonicals: list[str] = []
        self.citation_regions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            name = (values.get("name") or values.get("property") or "").casefold()
            content = _clean(values.get("content", ""))
            if name and content:
                self.meta.setdefault(name, []).append(content)
                if name.startswith("citation_") or name.startswith("dc.type"):
                    self.citation_regions.append(self.get_starttag_text())
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


class _PublicSections(HTMLParser):
    """Small HTML tree for explicit public regions, never whole-body abstracts."""
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.offsets = [0]
        for match in re.finditer("\n", html):
            self.offsets.append(match.end())
        self.root = {"tag": "root", "attrs": {}, "children": [], "start": 0, "end": len(html)}
        self.stack = [self.root]
        self.nodes = []

    def _offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag.casefold(), "attrs": dict(attrs), "children": [],
                "start": self._offset(), "end": None, "parent": self.stack[-1]}
        self.stack[-1]["children"].append(node)
        self.nodes.append(node)
        if tag not in self._void:
            self.stack.append(node)
        else:
            node["end"] = node["start"] + len(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.stack[-1]["end"] = self._offset() + len(self.get_starttag_text())
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                end = self.html.find(">", self._offset()) + 1
                for node in self.stack[index:]:
                    node["end"] = end
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)

    def text(self, node, skip_headings=False):
        attrs = node["attrs"]
        if (node["tag"] in {"script", "style", "noscript", "template"}
                or "hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true"
                or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(attrs.get("style", "")), re.I)
                or skip_headings and re.fullmatch(r"h[1-6]", node["tag"])):
            return ""
        return _clean(" ".join(child if isinstance(child, str) else self.text(child, skip_headings)
                               for child in node["children"]))

    def visible(self, node):
        current = node
        while current is not None:
            attrs = current["attrs"]
            if ("hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true"
                    or current["tag"] in {"template", "script", "style", "noscript"}
                    or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(attrs.get("style", "")), re.I)):
                return False
            current = current.get("parent")
        return True

    def region(self, node):
        if node["end"] is None:
            return ""
        region = self.html[node["start"]:node["end"]]
        return region if len(region.encode()) <= 100_000 else ""


def _parse_public_abstract(html: str, metadata: _LandingMetadata | None = None) -> tuple[str | None, str | None, str | None]:
    page = metadata or _LandingMetadata()
    if metadata is None:
        page.feed(html)
    abstract = page.one("citation_abstract")
    if abstract:
        region = next((tag for tag in page.citation_regions
                       if re.search(r"(?:name|property)=[\"']citation_abstract[\"']", tag, re.I)), "")
        if len(region.encode()) <= 100_000:
            return abstract, region, "publisher_landing_citation_metadata"
    tree = _PublicSections(html)
    tree.feed(html)
    candidates = []
    heading_nodes = [node for node in tree.nodes if re.fullmatch(r"h[1-6]", node["tag"])]
    heading_starts = [node["start"] for node in heading_nodes]
    for node in tree.nodes:
        attrs = node["attrs"]
        identifier = str(attrs.get("id") or "").casefold()
        explicit = identifier in {"abs1-content", "abstract-content", "abstract"}
        if node["tag"] == "section":
            headings = heading_nodes[bisect_right(heading_starts, node["start"]):
                bisect_right(heading_starts, node["end"] or node["start"])]
            explicit |= bool(headings and _normalize(tree.text(headings[0])) == "abstract")
            explicit |= str(attrs.get("data-title") or "").casefold() == "abstract"
        if explicit and tree.visible(node) and tree.region(node):
            # A container spanning the next major section is not an abstract region.
            other_major_heading = any(child["tag"] in {"h1", "h2"}
                and _normalize(tree.text(child)) != "abstract" for child in
                heading_nodes[bisect_right(heading_starts, node["start"]):
                              bisect_right(heading_starts, node["end"])])
            if not other_major_heading:
                candidates.append(node)
    # Prefer the narrowly labeled Nature abstract content region over its section.
    candidates.sort(key=lambda node: (str(node["attrs"].get("id") or "").casefold() != "abs1-content",
                                      node["end"] - node["start"]))
    for node in candidates:
        abstract = tree.text(node, skip_headings=True)
        if abstract and _normalize(abstract) != "abstract":
            return abstract, tree.region(node), "publisher_public_abstract_section"
    return None, None, None


def _publication_stage(page: _LandingMetadata, html: str) -> tuple[str, str | None]:
    # Do not infer a final version from an Article label, DOI or online date.
    values = [page.one("citation_version", "citation_publication_stage", "article_version")]
    tree = _PublicSections(html)
    tree.feed(html)
    for node in tree.nodes:
        marker = " ".join(str(node["attrs"].get(key) or "") for key in ("class", "id", "data-test"))
        if re.search(r"(?:article|publication)[-_](?:version|status|banner|notice)|(?:version|status)[-_](?:article|publication)", marker, re.I):
            text = tree.text(node)
            if len(text) <= 500 and tree.visible(node):
                values.append(text)
    for value in values:
        if (re.search(r"accepted\s+(?:article|manuscript)|author(?:'s)?\s+accepted", value, re.I)
                or "unedited" in value.casefold() and re.search(r"accepted for publication|before final publication", value, re.I)):
            return "accepted_manuscript", value
    for value in values:
        if re.search(r"version\s+of\s+record|final\s+(?:published\s+)?version", value, re.I):
            return "version_of_record", value
        if re.search(r"advance\s+online\s+publication|early\s+(?:view|access)", value, re.I):
            return "advance_online_publication", value
    return "unspecified", None


def _verify_landing(item: DigestItem, html: str, response_url: str) -> DigestItem | None:
    expected_url = _canonical_article_url(item.url)
    if _canonical_article_url(response_url) != expected_url:
        raise NatureVerificationError("Landing response changed the canonical article")
    page = _LandingMetadata()
    page.feed(html)
    for url in [*page.canonicals, *page.meta.get("citation_fulltext_html_url", []),
                *page.meta.get("citation_abstract_html_url", [])]:
        if _canonical_article_url(urljoin(response_url, url)) != expected_url:
            raise NatureVerificationError("Landing canonical metadata does not match the discovery article")
    title = page.one("citation_title")
    venue = page.one("citation_journal_title")
    if not title or not venue:
        raise NatureVerificationError("Landing page lacks citation_title or citation_journal_title")
    if _normalize(title) in {"", "untitled"} or _normalize(title) != _normalize(item.title):
        raise NatureVerificationError("Landing citation title does not match the discovery candidate")
    if venue.casefold() not in _VENUES or _normalize(venue) != _normalize(item.raw.get("venue")):
        raise NatureVerificationError("Landing citation journal does not match the discovery journal")
    doi_value = page.one("citation_doi")
    doi = _doi(doi_value)
    if not doi:
        raise NatureVerificationError("Landing citation DOI is missing or malformed")
    if doi.casefold() != ("10.1038/" + expected_url.rsplit("/", 1)[-1]).casefold():
        raise NatureVerificationError("Landing citation DOI does not match the Nature article identity")
    if doi and item.doi and doi.casefold() != item.doi.casefold():
        raise NatureVerificationError("Landing citation DOI does not match the discovery candidate")
    published_value = page.one("citation_online_date", "citation_publication_date", "citation_date")
    published = _date(published_value)
    if not published:
        raise NatureVerificationError("Landing citation publication date is missing, coarse or malformed")
    article_type = page.one("citation_article_type", "dc.type", "dc.type.article", "article_type")
    if _normalize_type(article_type) in _NON_PAPERS:
        item.raw["publisher_article_type"] = article_type
        return None
    item.title, item.url, item.doi = title, expected_url, doi
    item.published_at = published
    item.updated_at = published
    item.authors = page.meta.get("citation_author", []) or item.authors
    abstract, abstract_region, abstract_source = _parse_public_abstract(html, page)
    if abstract:
        item.abstract = abstract
        item.raw.update(abstract_source=abstract_source, abstract_verified=True,
                        abstract_evidence_region=abstract_region,
                        abstract_sha256=hashlib.sha256(abstract.encode()).hexdigest())
    else:
        item.raw["abstract_verified"] = False
    if sum(len(region.encode()) for region in page.citation_regions) + len((abstract_region or "").encode()) > 100_000:
        raise NatureVerificationError("Publisher metadata evidence exceeds the 100 KB packet limit")
    stage, stage_evidence = _publication_stage(page, html)
    item.raw.update(publication_stage=stage, publication_stage_evidence=stage_evidence,
                    publication_date_precision="day", publication_date_basis=(
                        "citation_online_date" if page.one("citation_online_date") else
                        "citation_publication_date" if page.one("citation_publication_date") else "citation_date"),
                    primary_metadata_evidence_regions=page.citation_regions,
                    publication_date_source_url=expected_url)
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
    if "publication date" in reason.casefold():
        item.raw["publication_date_status"] = "date_unresolved"
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
