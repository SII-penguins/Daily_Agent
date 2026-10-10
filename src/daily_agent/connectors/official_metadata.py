"""Bounded public metadata support; never a full-document reading cache."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from daily_agent.discovery_window import lookback_start
from daily_agent.scoring.relevance import passes_topic_gate, topic_relevance_score
from daily_agent.scoring.topics import quantum_topic
from daily_agent.workflow_state import atomic_json, read_json, StateCorrupt

SCHEMA = 1


class MetadataLimit(ValueError):
    """A visible incomplete result, not a negative finding about a paper."""


def discovery_bounds(config, target_date=None, window_days=None):
    target = target_date or datetime.now(timezone.utc)
    day = target.date() if isinstance(target, datetime) else target
    start = lookback_start(config, day)
    if start is None:
        days = max(0, int(window_days if window_days is not None else 92))
        start = day - timedelta(days=days)
    return start, day


def exact_date_in_window(value, bounds):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return bounds[0] <= date.fromisoformat(value) <= bounds[1]
    except ValueError:
        return False


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


class MetadataCache:
    """Cross-day parser-bound evidence, including explicit failed attempts.

    No date/topic decision is cached as eligibility. Blocked entries do not
    expire automatically; an operator must explicitly disable/clear them after
    a legitimate access change. Pending budget work is never negative-cached.
    """
    def __init__(self, config, source, enabled=True):
        settings = config.sources.get(source, {}) or {}
        self.enabled = enabled and bool(settings.get("metadata_cache_enabled", False))
        self.root = config.root / "data" / "cache" / "official-metadata" / "v1" / source
        self.source = source
        self.max_entries = min(20000, max(1, int(settings.get("metadata_cache_max_entries", 2000))))
        self.max_bytes = min(256 * 1024**2, max(1024, int(settings.get("metadata_cache_max_bytes", 128 * 1024**2))))

    def _path(self, url, kind):
        return self.root / (fingerprint([SCHEMA, self.source, kind, url]) + ".json")

    def get(self, url, kind="landing"):
        if not self.enabled:
            return None
        path = self._path(url, kind)
        if path.exists() and path.stat().st_size > self.max_bytes:
            raise StateCorrupt("Official metadata cache entry exceeds capacity")
        entry = read_json(path)
        if entry is None:
            return None
        if (not isinstance(entry, dict) or entry.get("schema") != SCHEMA
                or entry.get("identity") != [self.source, kind, url]
                or entry.get("sha256") != fingerprint({key: value for key, value in entry.items() if key != "sha256"})):
            raise StateCorrupt("Official metadata cache integrity/identity mismatch")
        expires = entry.get("expires_at")
        if expires is not None and time.time() >= expires:
            return None
        return entry

    def put(self, url, payload, kind="landing", status="verified", ttl_seconds=None):
        if not self.enabled or status in {"pending", "budget_exhausted"}:
            return
        if ttl_seconds is None:
            ttl_seconds = {"verified": 30 * 86400, "index": 86400,
                           "metadata_missing": 48 * 3600, "not_found": 86400,
                           "identity_mismatch": 86400, "rate_limited": 6 * 3600,
                           "transient_error": 3600, "response_too_large": 86400}.get(status, 86400)
        entry = {"schema": SCHEMA, "identity": [self.source, kind, url], "status": status,
                 "checked_at": datetime.now(timezone.utc).isoformat(),
                 "expires_at": None if status == "blocked" else time.time() + max(0, ttl_seconds),
                 "payload": payload}
        entry["sha256"] = fingerprint(entry)
        encoded_bytes = len(json.dumps(entry, ensure_ascii=False).encode())
        # Do not evict issue evidence or mutate other cache entries implicitly.
        # A full cache is visible and can be pruned explicitly by its operator.
        files = list(self.root.glob("*.json")) if self.root.exists() else []
        target = self._path(url, kind)
        total = sum(p.stat().st_size for p in files if p != target)
        if encoded_bytes + total > self.max_bytes or (not target.exists() and len(files) >= self.max_entries):
            raise MetadataLimit("Official metadata cache capacity reached")
        atomic_json(target, entry)


class MetadataBudget:
    def __init__(self, max_requests=42, max_bytes=24 * 1024**2, seconds=150):
        self.max_requests = max(0, int(max_requests))
        self.max_bytes = max(0, int(max_bytes))
        self.requests = 0
        self.bytes = 0
        self.deadline = time.monotonic() + max(0, float(seconds))

    def reserve(self):
        if self.requests >= self.max_requests:
            raise MetadataLimit("metadata_request_budget_exhausted")
        if self.bytes >= self.max_bytes:
            raise MetadataLimit("metadata_byte_budget_exhausted")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataLimit("metadata_time_budget_exhausted")
        self.requests += 1
        return min(8.0, remaining)


def fetch_text(client, url, budget, max_bytes, validate, expected_types=("text/html",)):
    """Stream a single allowlisted URL. Redirects remain visible, never followed."""
    validate(url)
    timeout = budget.reserve()
    with client.stream("GET", url, follow_redirects=False,
                       timeout=httpx.Timeout(timeout, connect=min(3.0, timeout))) as response:
        response.raise_for_status()
        validate(str(response.url))
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type and not any(content_type == kind for kind in expected_types):
            raise ValueError("Unexpected public metadata content type")
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise MetadataLimit("metadata_response_too_large")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            budget.bytes += len(chunk)
            if len(content) + len(chunk) > max_bytes:
                raise MetadataLimit("metadata_response_too_large")
            if budget.bytes > budget.max_bytes:
                raise MetadataLimit("metadata_byte_budget_exhausted")
            if time.monotonic() >= budget.deadline:
                raise MetadataLimit("metadata_time_budget_exhausted")
            content.extend(chunk)
    return content.decode("utf-8", errors="replace"), str(response.url), hashlib.sha256(content).hexdigest()


def failure_status(exc):
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code in {401, 403} or code is not None and 300 <= code < 400:
        return "blocked"
    if code == 429:
        return "rate_limited"
    if code == 404:
        return "not_found"
    if isinstance(exc, MetadataLimit):
        return "response_too_large" if "response_too_large" in str(exc) else "budget_exhausted"
    if isinstance(exc, httpx.HTTPError):
        return "transient_error"
    return "identity_mismatch"


def discovery_relevant(item, config):
    # Recall hints buy only metadata lookup, never bypass the final topic gate.
    return passes_topic_gate(item, config) or bool(re.search(
        r"quantum|qubit|\bqec\b|\bqnn\b|hamiltonian|tensor[ -]network", item.title, re.I))


def fair_candidate_order(items, config):
    """Fair lookup opportunities across interests; no final venue/report quota."""
    groups = defaultdict(list)
    for item in items:
        direction, subtopic = quantum_topic(item)
        if direction:
            group = direction
            item.quota_group = "quantum"
        elif re.search(r"hamiltonian|tensor[ -]network", item.title, re.I):
            group, subtopic = "ai_for_quantum", "uncertain"
        else:
            group, subtopic = "embodied_and_agents", "general"
        item.raw["discovery_group"] = group
        item.raw["discovery_subtopic"] = subtopic
        groups[group].append(item)
    queues = {}
    for group, rows in groups.items():
        # Domain breadth first; high global scores cannot consume another group.
        topics = defaultdict(deque)
        for item in sorted(rows, key=lambda x: (-topic_relevance_score(x, config), x.url)):
            topics[item.raw["discovery_subtopic"]].append(item)
        queue = deque()
        while any(topics.values()):
            for topic in sorted(topics):
                if topics[topic]:
                    queue.append(topics[topic].popleft())
        queues[group] = queue
    result = []
    # Existing config has 6 quantum / 4 exploratory slots; split quantum evenly.
    quantum = max(1, int(config.quota.get("quantum_target", 6)))
    exploratory = max(1, int(config.quota.get("exploratory_target", 4)))
    weights = {"ai_for_quantum": max(1, quantum // 2),
               "quantum_for_ai": max(1, quantum - quantum // 2),
               "embodied_and_agents": exploratory}
    while any(queues.values()):
        for group, weight in weights.items():
            for _ in range(weight):
                if queues.get(group):
                    result.append(queues[group].popleft())
    return result


def failure_ttl(exc):
    """Honor an explicit rate-limit cooldown; do not sleep/retry this run."""
    response = getattr(exc, 'response', None)
    if getattr(response, 'status_code', None) != 429:
        return None
    value = response.headers.get('retry-after', '')
    try:
        seconds = float(value)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            stamp = parsedate_to_datetime(value)
            seconds = stamp.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 0
    return max(6 * 3600, seconds)
