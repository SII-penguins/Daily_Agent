from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import DigestItem

OPENREVIEW_NOTES_URL = "https://api2.openreview.net/notes"
OPENREVIEW_BASE_URL = "https://openreview.net"


class OpenReviewAccessError(httpx.HTTPStatusError):
    """Explicit source outage, never an empty successful collection."""



def fetch_openreview(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("openreview", {}) or {}
    if not source_config.get("enabled", False):
        return []
    max_results = int(source_config.get("max_results_per_venue", source_config.get("max_results_per_query", 25)))
    timeout = float(source_config.get("timeout_seconds", 30))
    venues = source_config.get("venues", []) or []
    items: list[DigestItem] = []
    errors = []
    successful_requests = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for venue in venues:
            invitation = str(venue.get("invitation") or "")
            venue_id = str(venue.get("venueid") or "")
            if not invitation and not venue_id:
                continue
            try:
                response = client.get(OPENREVIEW_NOTES_URL, params={**({"content.venueid": venue_id} if venue_id else {"invitation": invitation}), "limit": max_results, "sort": "tcdate:desc"})
                if response.status_code in (401, 403):
                    raise OpenReviewAccessError(
                        f"OpenReview HTTP {response.status_code}: access denied for {venue_id or invitation}; "
                        "check service access/network policy or authorized authentication. "
                        "Other sources do not establish OpenReview review availability.",
                        request=httpx.Request("GET", OPENREVIEW_NOTES_URL), response=response)
                response.raise_for_status()
                payload = response.json()
                successful_requests += 1
            except (httpx.HTTPError, ValueError, OpenReviewAccessError) as exc:
                errors.append(exc)
                continue
            for note in payload.get("notes", []) or []:
                item = _note_to_item(note, str(venue.get("name") or invitation))
                if source_config.get("accepted_only", False) and item.raw["publication_status"] != "accepted":
                    continue
                if _matches_interest(item, config):
                    items.append(item)
    if errors and not successful_requests:
        raise errors[-1]
    return items


def _note_to_item(note: dict[str, Any], venue: str) -> DigestItem:
    content = note.get("content") or {}
    note_id = str(note.get("id") or "")
    title = _content_value(content, "title") or "untitled"
    abstract = _content_value(content, "abstract")
    authors = _content_list(content, "authors")
    pdf = _content_value(content, "pdf")
    published_at = _date_from_millis(note.get("pdate") or note.get("cdate") or note.get("tcdate"))
    forum_url = f"{OPENREVIEW_BASE_URL}/forum?id={note_id}" if note_id else OPENREVIEW_BASE_URL
    return DigestItem(
        id=note_id,
        source="openreview",
        item_type="paper",
        title=title,
        url=forum_url,
        pdf_url=urljoin(OPENREVIEW_BASE_URL, pdf) if pdf else None,
        authors=authors,
        abstract=abstract,
        published_at=published_at,
        updated_at=published_at,
        source_tags=["openreview", venue],
        categories=[venue],
        raw={
            "openreview_id": note_id,
            "openreview_url": forum_url,
            "venue": _content_value(content, "venue") or venue,
            "venueid": _content_value(content, "venueid"),
            "publication_status": _publication_status(content),
        },
    )


def _content_value(content: dict[str, Any], key: str) -> str | None:
    value = content.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return str(value) if value else None


def _content_list(content: dict[str, Any], key: str) -> list[str]:
    value = content.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def _date_from_millis(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _matches_interest(item: DigestItem, config: AppConfig) -> bool:
    text = " ".join([item.title, item.abstract or "", " ".join(item.categories)]).lower()
    for domain in config.domains:
        if any(keyword.lower() in text for keyword in domain.include_keywords):
            item.quota_group = domain.quota_group
            item.source_tags.append(domain.name)
            return True
    return True


def _publication_status(content):
    venue = _content_value(content, 'venue') or ''
    venueid = _content_value(content, 'venueid') or ''
    import re
    if re.search(r'reject|withdraw', venue+' '+venueid, re.I): return 'rejected'
    if re.search(r'submitted|submission|under.review', venue+' '+venueid, re.I): return 'submitted'
    if re.fullmatch(r'(?:ICLR|ICML|NeurIPS)\.cc/20\d{2}/Conference', venueid): return 'accepted'
    if re.search(r'\b(?:ICLR|ICML|NeurIPS)\s+20\d{2}\s+(?:poster|oral|spotlight)\b', venue, re.I): return 'accepted'
    return 'unconfirmed'
