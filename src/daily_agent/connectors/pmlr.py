from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem
from daily_agent.connectors.source_failures import failure_record, finish_collection


def fetch_pmlr(config: AppConfig, target_date: datetime | None = None,
               window_days: int | None = None, *, budget=None, cache=None,
               coverage=None) -> list[DigestItem]:
    """Discover the complete bounded index before fair, cached abstract lookup."""
    from daily_agent.connectors.official_metadata import (
        MetadataBudget, MetadataCache, MetadataLimit, discovery_bounds,
        exact_date_in_window, fetch_text, failure_status, failure_ttl, discovery_relevant,
        fair_candidate_order,
    )
    from daily_agent.scoring.relevance import passes_topic_gate
    settings = config.sources.get("pmlr", {}) or {}
    if not settings.get("enabled", False):
        return []
    bounds = discovery_bounds(config, target_date, window_days)
    budget = budget or MetadataBudget(min(42, int(settings.get("max_metadata_requests", 42))),
        min(24 * 1024**2, int(settings.get("max_metadata_bytes", 24 * 1024**2))),
        min(150, float(settings.get("metadata_budget_seconds", 150))))
    cache = cache or MetadataCache(config, "pmlr")
    coverage = coverage if coverage is not None else {}
    coverage.update(discovered=0, eligible=0, cache_hits=0, enriched=0, pending=0,
                    skipped_old_volumes=0, complete=True, window=[str(x) for x in bounds])
    max_results = max(0, int(settings.get("max_results_per_volume", 100)))
    detail_limit = max(0, min(100, int(settings.get("max_detail_pages", 40))))
    volume_limit = max(0, min(8, int(settings.get("max_index_pages", 2))))
    entries_limit = max(1, min(20000, int(settings.get("max_index_entries", 10000))))
    candidates, failures, errors = [], [], []
    successes = details = 0
    host_blocked = False
    volumes = settings.get("volumes", []) or []
    with httpx.Client(timeout=min(8, float(settings.get("timeout_seconds", 8))),
                      follow_redirects=False, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for position, volume in enumerate(volumes):
            url = str(volume.get("url") or "")
            if not url:
                continue
            if position >= volume_limit:
                coverage.update(complete=False, stop_reason="index_page_budget_exhausted")
                break
            cached = None
            try:
                _validate_pmlr_url(url, article=False)
                cached = cache.get(url, kind="index")
                if cached:
                    if cached["status"] != "index":
                        if cached["status"] in {"blocked", "rate_limited"}: host_blocked = True
                        raise ValueError("Cached PMLR index failure: " + cached["payload"].get("reason", cached["status"]))
                    rows = [DigestItem.from_dict(row) for row in cached["payload"]["items"]]
                    coverage["cache_hits"] += 1
                elif host_blocked:
                    coverage.update(complete=False, stop_reason="host_access_denied_or_rate_limited")
                    continue
                else:
                    html, final, sha = fetch_text(client, url, budget,
                        min(16 * 1024**2, int(settings.get("max_index_bytes", 16 * 1024**2))),
                        lambda value: _validate_pmlr_url(value, article=False))
                    declared = re.search(r"Published\s+as\s+Volume\s+(\d+)", html, re.I)
                    if declared and "/v" + declared[1] + "/" != urlsplit(url).path:
                        raise ValueError("PMLR index volume identity mismatch")
                    venue = str(volume.get("venue") or _venue_from_url(url))
                    rows = _parse_volume(html, final, venue)
                    if len(rows) > entries_limit:
                        raise MetadataLimit("PMLR index entry capacity exceeded; no truncated complete index")
                    if not rows and not declared:
                        raise ValueError("PMLR index contained paper markup but yielded no titles")
                    for item in rows:
                        _validate_pmlr_url(item.url, article=True, volume_url=url)
                        item.raw.update(official_metadata_status="official_index_verified",
                            index_evidence={"url": url, "response_sha256": sha,
                                            "title": item.title, "venue": venue},
                            publication_type="proceedings-article")
                    cache.put(url, {"items": [item.to_dict() for item in rows]}, kind="index", status="index")
                successes += 1
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failures.append(cached["payload"].get("failure", failure_record(url, exc))
                    if cached and cached["status"] != "index" else failure_record(url, exc))
                coverage.update(complete=False, stop_reason=str(exc))
                if failure_status(exc) in {"blocked", "rate_limited"} or str(exc).startswith(("Cached PMLR index failure: blocked", "Cached PMLR index failure: rate_limited")):
                    host_blocked = True
                if not str(exc).startswith("Cached"):
                    cache.put(url, {"reason": str(exc), "failure": failure_record(url, exc)}, kind="index",
                              status=failure_status(exc), ttl_seconds=failure_ttl(exc))
                continue
            coverage["discovered"] += len(rows)
            # Coarse/unknown publication dates remain cached leads, never recent papers.
            eligible = [item for item in rows if exact_date_in_window(item.published_at, bounds)
                        or item.raw.get("publication_date_precision") != "day"]
            if rows and not eligible:
                coverage["skipped_old_volumes"] += 1
            for item in eligible:
                if discovery_relevant(item, config):
                    item.raw["pmlr_volume_url"] = url
                    candidates.append(item)
        ordered = fair_candidate_order(candidates, config)
        enriched = []
        for item in ordered:
            try:
                cached = cache.get(item.url)
                # A corrected official index invalidates content-bound metadata,
                # not the URL's access-denial/rate-limit state. Revalidate once
                # within the ordinary detail/request budget instead of caching
                # the stale comparison itself as a new fetch failure.
                if cached and cached["status"] in {"verified", "metadata_missing", "identity_mismatch"}:
                    payload = cached["payload"]
                    cached_item = payload.get("item") or {}
                    old_doi = payload.get("input_doi") or cached_item.get("doi")
                    changed = (payload.get("input_title") != item.title
                        or bool(item.doi and old_doi and item.doi.casefold() != str(old_doi).casefold()))
                    if changed:
                        cached = None
                        coverage["identity_cache_misses"] = coverage.get("identity_cache_misses", 0) + 1
                if cached:
                    coverage["cache_hits"] += 1
                    payload = cached["payload"]
                    if payload.get("item") and payload.get("input_title") == item.title:
                        item = DigestItem.from_dict(payload["item"])
                        item.raw["metadata_checked_at"] = cached["checked_at"]
                        item.raw["metadata_cache_hit"] = True
                    elif cached["status"] != "verified":
                        item.raw["abstract_status"] = cached["status"]
                        item.raw["metadata_failure"] = payload
                        error = ValueError("Cached PMLR metadata failure: " + payload.get("reason", cached["status"]))
                        errors.append(error)
                        failures.append({"target": item.url, "type": cached["status"],
                            "status_code": payload.get("status_code"), "message": str(error),
                            "cached": True, "checked_at": cached["checked_at"]})
                        coverage.update(complete=False, stop_reason=cached["status"])
                        if cached["status"] in {"blocked", "rate_limited"}: host_blocked = True
                    else:
                        raise ValueError("Cached PMLR identity no longer matches index")
                elif details >= detail_limit or host_blocked:
                    item.raw["abstract_status"] = "enrichment_pending"
                    coverage["pending"] += 1
                else:
                    details += 1
                    html, final, sha = fetch_text(client, item.url, budget,
                        min(512 * 1024, int(settings.get("max_landing_bytes", 512 * 1024))),
                        lambda value: _validate_pmlr_url(value, article=True,
                            volume_url=item.raw["pmlr_volume_url"]))
                    item = _parse_paper_metadata(item, html, final, sha)
                    coverage["enriched"] += 1
                    cache.put(item.url, {"input_title": item.title, "input_doi": item.doi, "item": item.to_dict()},
                              status="verified" if item.abstract else "metadata_missing")
                enriched.append(item)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc); failures.append(failure_record(item.url, exc))
                status = failure_status(exc)
                if status in {"blocked", "rate_limited"}: host_blocked = True
                item.raw.update(abstract_status=status, metadata_failure={"reason": str(exc)})
                cache.put(item.url, {"reason": str(exc), "input_title": item.title, "input_doi": item.doi,
                    "status_code": getattr(getattr(exc, "response", None), "status_code", None)}, status=status, ttl_seconds=failure_ttl(exc))
                enriched.append(item)
                coverage.update(complete=False, stop_reason=str(exc))
        items, counts = [], {}
        for item in fair_candidate_order(enriched, config):
            if not exact_date_in_window(item.published_at, bounds) or not passes_topic_gate(item, config):
                continue
            coverage["eligible"] += 1
            volume = item.raw.get("pmlr_volume_url", "")
            if counts.get(volume, 0) < max_results:
                items.append(item); counts[volume] = counts.get(volume, 0) + 1
        if coverage["pending"]:
            coverage.update(complete=False, stop_reason="detail_page_budget_exhausted")
        coverage.update(returned=len(items), requests=budget.requests, bytes=budget.bytes,
                        new_landing_requests=details)
    return finish_collection("PMLR", items, failures, errors, successes)


def _validate_pmlr_url(url, *, article, volume_url=None):
    parsed = urlsplit(url)
    pattern = r"/v[0-9]+/[A-Za-z0-9._-]+\.html" if article else r"/v[0-9]+/"
    if (parsed.scheme != "https" or parsed.hostname != "proceedings.mlr.press"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or not re.fullmatch(pattern, parsed.path)):
        raise ValueError("Expected canonical official PMLR metadata URL")
    if volume_url and not url.startswith(volume_url):
        raise ValueError("PMLR landing escaped the expected volume")
    return url


def _parse_paper_metadata(item, html, response_url, sha):
    # Reuse the strict citation parser; no page-body or fulltext fallback.
    from daily_agent.connectors.nature import _LandingMetadata, _normalize, _date, _doi
    _validate_pmlr_url(response_url, article=True, volume_url=item.raw.get("pmlr_volume_url"))
    if response_url != item.url:
        raise ValueError("PMLR response changed article identity")
    page = _LandingMetadata(); page.feed(html)
    for url in [*page.canonicals, *page.meta.get("citation_abstract_html_url", [])]:
        if urljoin(response_url, url) != item.url:
            raise ValueError("PMLR canonical identity mismatch")
    title = page.one("citation_title")
    conference = page.one("citation_conference_title", "citation_inbook_title")
    if not title or _normalize(title) != _normalize(item.title) or not conference:
        raise ValueError("PMLR citation title/conference metadata missing or mismatched")
    expected = str(item.raw.get("venue", ""))
    if "ICML" in expected.upper() and not re.search(r"international conference on machine learning|\bicml\b", conference, re.I):
        raise ValueError("PMLR citation conference conflicts with expected ICML venue")
    published = _date(page.one("citation_publication_date"))
    if published and item.raw.get("publication_date_precision") == "day" and published != item.published_at:
        raise ValueError("PMLR landing date conflicts with official volume release")
    if published and item.raw.get("publication_date_precision") != "day":
        item.published_at = published
        item.raw.update(publication_date=published, publication_date_precision="day",
            publication_date_basis="official_landing_citation", publication_date_source_url=item.url)
    doi = _doi(page.one("citation_doi"))
    if doi and item.doi and doi.casefold() != item.doi.casefold():
        raise ValueError("PMLR citation DOI mismatch")
    item.authors = page.meta.get("citation_author", []) or item.authors
    item.doi = doi or item.doi
    abstract = page.one("citation_abstract") or _public_abstract(html)
    if abstract and len(abstract) > 20000:
        raise ValueError("PMLR abstract exceeds metadata capacity")
    item.abstract = abstract or None
    item.raw.update(official_metadata_status="landing_metadata_verified",
        abstract_status="abstract_verified" if abstract else "abstract_not_public",
        abstract_source="official_pmlr_abstract" if abstract else None,
        primary_landing_verified=True, publication_status="published",
        primary_verification={"verified": True, "method": "official_proceedings_metadata",
            "url": item.url, "title": title, "venue": expected, "conference": conference,
            "published_at": item.published_at, "response_sha256": sha},
        metadata_evidence={"source_url": item.url, "response_sha256": sha,
            "citation_title": title, "citation_conference_title": conference,
            "citation_publication_date": page.one("citation_publication_date"),
            "abstract": abstract,
            "citation_excerpt": "\n".join(tag for tag in re.findall(r"<meta\b[^>]*>", html, re.I)
                if re.search(r"citation_(?:title|conference_title|inbook_title|publication_date)", tag, re.I))})
    return item


def _public_abstract(html):
    from html.parser import HTMLParser
    class Abstract(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True); self.depth = 0; self.parts = []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if self.depth:
                if tag not in {"br", "img", "hr", "input", "meta", "link", "wbr"}: self.depth += 1
            elif tag == "div" and (attrs.get("id") == "abstract" or "abstract" in attrs.get("class", "").split()):
                self.depth = 1
        def handle_endtag(self, tag):
            if self.depth: self.depth -= 1
        def handle_data(self, data):
            if self.depth: self.parts.append(data)
    parser = Abstract(); parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip() or None


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
    return False
