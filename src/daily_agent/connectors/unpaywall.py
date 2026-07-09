from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import MaterialRecord
from daily_agent.secrets import credential_value

UNPAYWALL_URL = "https://api.unpaywall.org/v2"


def enrich_unpaywall_links(records: list[MaterialRecord], config: AppConfig) -> list[MaterialRecord]:
    source_config = config.sources.get("unpaywall", {}) or {}
    if not source_config.get("enabled", True):
        return records
    email_env = str(source_config.get("email_env") or "UNPAYWALL_EMAIL")
    email = credential_value(email_env)
    max_papers = int(source_config.get("max_papers_per_run", 30))
    timeout = float(source_config.get("timeout_seconds", 15))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    if max_papers <= 0:
        return records
    cache_enabled = bool(source_config.get("cache_enabled", True))
    cache = _load_cache(_unpaywall_cache_path(config, source_config)) if cache_enabled else {}
    candidates = [
        record
        for record in records
        if record.item_type == "paper"
        and record.doi
        and not (record.raw or {}).get("unpaywall_resolver")
        and (not record.pdf_url or not (record.raw or {}).get("open_access_url"))
    ][:max_papers]
    if not candidates:
        return records
    misses: list[MaterialRecord] = []
    for record in candidates:
        doi = _normalize_doi(record.doi)
        entry = (cache.get("items", {}) or {}).get(doi or "") if cache_enabled else None
        if _apply_cache_entry(record, entry):
            continue
        misses.append(record)
    if not email or not misses:
        return records
    deadline = time.monotonic() + run_budget_seconds if run_budget_seconds > 0 else None
    changed = False
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for record in misses:
            if _deadline_exceeded(deadline):
                _set_resolver_status(record, "skipped", reason="run_budget_exceeded")
                continue
            cache_entry = _resolve_record(client, record, email)
            if cache_entry and cache_enabled:
                _store_cache_entry(cache, cache_entry, max_entries=int(source_config.get("cache_max_entries", 50_000)))
                changed = True
    if changed:
        _write_cache(_unpaywall_cache_path(config, source_config), cache)
    return records


def _resolve_record(client: httpx.Client, record: MaterialRecord, email: str) -> dict[str, Any] | None:
    doi = _normalize_doi(record.doi)
    if not doi:
        return None
    try:
        response = client.get(f"{UNPAYWALL_URL}/{doi}", params={"email": email})
        if response.status_code in {403, 404, 429}:
            _set_resolver_status(record, "unavailable", reason=f"http_{response.status_code}")
            return None
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        _set_resolver_status(record, "unavailable", reason="request_failed")
        return None
    if not isinstance(payload, dict):
        _set_resolver_status(record, "unavailable", reason="invalid_response")
        return None
    links = _extract_links(payload)
    if not any(links.values()):
        _set_resolver_status(record, "not_oa")
        return None
    _apply_links(record, payload, links, status="resolved")
    return {
        "doi": doi,
        "status": "resolved",
        "saved_at": _utc_now(),
        "payload": {"title": payload.get("title") or record.title, "doi": _normalize_doi(payload.get("doi")) or doi},
        "links": {key: value for key, value in links.items() if value},
    }


def _extract_links(payload: dict[str, Any]) -> dict[str, str | None]:
    best_oa = _as_dict(payload.get("best_oa_location"))
    pdf_url = _string_or_none(best_oa.get("url_for_pdf"))
    landing_url = _string_or_none(best_oa.get("url_for_landing_page")) or _string_or_none(best_oa.get("url"))
    oa_url = pdf_url or landing_url
    return {
        "pdf_url": pdf_url if pdf_url and _is_http_url(pdf_url) else None,
        "landing_url": landing_url if landing_url and _is_http_url(landing_url) and not _looks_like_pdf_url(landing_url) else None,
        "open_access_url": oa_url if oa_url and _is_http_url(oa_url) else None,
        "host_type": _string_or_none(best_oa.get("host_type")),
        "license": _string_or_none(best_oa.get("license")),
        "version": _string_or_none(best_oa.get("version")),
    }


def _merge_unpaywall_evidence(record: MaterialRecord, payload: dict[str, Any], links: dict[str, str | None]) -> None:
    sources = record.evidence.setdefault("sources", {})
    existing = sources.get("unpaywall") if isinstance(sources.get("unpaywall"), dict) else {}
    sources["unpaywall"] = {
        **existing,
        "title": payload.get("title") or record.title,
        "url": links.get("landing_url") or links.get("open_access_url") or existing.get("url"),
        "landing_url": links.get("landing_url") or existing.get("landing_url"),
        "pdf_url": links.get("pdf_url") or existing.get("pdf_url"),
        "open_access_url": links.get("open_access_url") or existing.get("open_access_url"),
        "doi": _normalize_doi(payload.get("doi")) or record.doi or existing.get("doi"),
        "host_type": links.get("host_type") or existing.get("host_type"),
        "license": links.get("license") or existing.get("license"),
        "version": links.get("version") or existing.get("version"),
    }


def _apply_links(record: MaterialRecord, payload: dict[str, Any], links: dict[str, str | None], status: str) -> None:
    if links.get("pdf_url") and not record.pdf_url:
        record.pdf_url = links["pdf_url"]
    if links.get("open_access_url"):
        record.raw["open_access_url"] = links["open_access_url"]
    if links.get("landing_url"):
        record.raw["unpaywall_url"] = links["landing_url"]
    _merge_unpaywall_evidence(record, payload, links)
    _set_resolver_status(record, status, **{key: value for key, value in links.items() if value})


def _apply_cache_entry(record: MaterialRecord, entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "resolved":
        return False
    links = entry.get("links")
    if not isinstance(links, dict) or not any(links.values()):
        return False
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    _apply_links(record, payload, links, status="cache_hit")
    return True


def _unpaywall_cache_path(config: AppConfig, source_config: dict[str, Any]) -> Path:
    configured = str(source_config.get("cache_path") or "data/cache/unpaywall.json")
    path = Path(configured)
    return path if path.is_absolute() else config.root / path


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "items": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "items": {}}
    items = payload.get("items")
    if not isinstance(items, dict):
        payload["items"] = {}
    payload.setdefault("schema_version", 1)
    return payload


def _store_cache_entry(cache: dict[str, Any], entry: dict[str, Any], max_entries: int) -> None:
    doi = entry.get("doi")
    if not isinstance(doi, str) or not doi:
        return
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items
    items[doi] = {key: value for key, value in entry.items() if key != "doi"}
    if max_entries > 0 and len(items) > max_entries:
        for key in sorted(items, key=lambda item: str((items.get(item) or {}).get("saved_at") or ""))[: len(items) - max_entries]:
            items.pop(key, None)


def _write_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**cache, "schema_version": 1, "updated_at": _utc_now()}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return


def _set_resolver_status(record: MaterialRecord, status: str, **details: Any) -> None:
    record.raw["unpaywall_resolver"] = {"status": status, **details}


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_doi(value: Any) -> str | None:
    text = _string_or_none(value)
    if not text:
        return None
    return text.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".pdf") or "/pdf/" in path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
