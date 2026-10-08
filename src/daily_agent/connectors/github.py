from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

import httpx

from daily_agent.config import AppConfig, DomainConfig
from daily_agent.models import DigestItem, MaterialRecord, SelectedRecord
from daily_agent.connectors.source_failures import failure_record, finish_collection
from daily_agent.secrets import credential_value

GITHUB_API = "https://api.github.com"
TRENDING_URL = "https://github.com/trending"


def fetch_github(config: AppConfig, target_date: datetime | None = None, window_days: int | None = None) -> list[DigestItem]:
    source_config = config.sources.get("github", {}) or {}
    if not source_config.get("enabled", True):
        return []

    target = target_date or datetime.now(timezone.utc)
    max_results = int(source_config.get("max_results_per_query", 10))
    search_window_days = int(window_days or source_config.get("normal_active_days", 30))
    max_queries_per_domain = int(source_config.get("max_queries_per_domain", 0) or 0)
    timeout_seconds = float(source_config.get("timeout_seconds", 30))
    items: list[DigestItem] = []
    failures, errors = [], []
    successes = 0
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=_headers()) as client:
        jobs = []
        if source_config.get("search_enabled", True):
            jobs.append(lambda: _fetch_search(client, config, target, max_results, search_window_days, max_queries_per_domain))
        if source_config.get("trending_enabled", True):
            jobs.append(lambda: _fetch_trending(client, config, target, max_results))
        for job in jobs:
            try:
                items.extend(job())
                successes += 1
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                errors.append(exc)
                items.extend(getattr(exc, "partial_items", []))
                failures.extend(getattr(exc, "failures", [failure_record("GitHub discovery", exc)]))
                successes += bool(getattr(exc, "partial_success", False))
                if any(row.get("status_code") in {401, 403, 429} for row in failures):
                    break
    return finish_collection("GitHub", items, failures, errors, successes)


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Daily-Agent/0.1"}
    token = credential_value("GITHUB_TOKEN") or credential_value("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_search(
    client: httpx.Client,
    config: AppConfig,
    target: datetime,
    max_results: int,
    window_days: int,
    max_queries_per_domain: int,
) -> list[DigestItem]:
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    seen_queries: set[str] = set()
    for domain in config.domains:
        for query in _search_queries(domain, max_queries_per_domain):
            if query in seen_queries:
                continue
            seen_queries.add(query)
            cutoff = (target - timedelta(days=window_days)).date().isoformat()
            search_query = f"{query} pushed:>={cutoff} sort:updated-desc"
            try:
                response = client.get(
                    f"{GITHUB_API}/search/repositories",
                    params={"q": search_query, "sort": "updated", "order": "desc", "per_page": max_results},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict): raise ValueError("Expected source JSON object")
                successful_requests += 1
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failures.append(failure_record(search_query, exc))
                if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                    return finish_collection("GitHub search", items, failures, errors, successful_requests)
                continue
            for repo in payload.get("items", []):
                item = _repo_to_item(repo, domain, "search")
                _mark_activity_window(item, config, target)
                items.append(item)
    return finish_collection("GitHub search", items, failures, errors, successful_requests)


def _search_queries(domain: DomainConfig, max_queries: int = 0) -> list[str]:
    if domain.github_queries:
        enabled_expanded = [keyword for keyword in domain.expanded_keywords if keyword in domain.include_keywords]
        queries = _interleave(domain.github_queries, enabled_expanded)
        queries.extend(domain.base_include_keywords)
    else:
        queries = domain.include_keywords or [domain.name]
    unique = _unique_queries(queries)
    return unique[:max_queries] if max_queries > 0 else unique


def _interleave(primary: list[str], secondary: list[str]) -> list[str]:
    result: list[str] = []
    longest = max(len(primary), len(secondary))
    for index in range(longest):
        if index < len(primary):
            result.append(primary[index])
        if index < len(secondary):
            result.append(secondary[index])
    return result


def _unique_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for query in queries:
        text = str(query).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _fetch_trending(
    client: httpx.Client,
    config: AppConfig,
    target: datetime,
    max_results: int,
) -> list[DigestItem]:
    items: list[DigestItem] = []
    failures, errors = [], []
    successful_requests = 0
    languages = config.sources.get("github", {}).get("trending_languages", [""])
    for language in languages:
        url = f"{TRENDING_URL}/{language}" if language else TRENDING_URL
        try:
            response = client.get(url, params={"since": "daily"}, headers={"User-Agent": "Daily-Agent/0.1"})
            response.raise_for_status()
            successful_requests += 1
        except httpx.HTTPError as exc:
            errors.append(exc)
            failures.append(failure_record(url, exc))
            if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                return finish_collection("GitHub trending", items, failures, errors, successful_requests)
            continue
        repos = _parse_trending_repos(response.text)[:max_results]
        for full_name in repos:
            try:
                repo_response = client.get(f"{GITHUB_API}/repos/{full_name}")
                repo_response.raise_for_status()
                repo_json = repo_response.json()
                if not isinstance(repo_json, dict): raise ValueError("Expected source JSON object")
                successful_requests += 1
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(exc)
                failures.append(failure_record(f"{GITHUB_API}/repos/{full_name}", exc))
                if getattr(getattr(exc, "response", None), "status_code", None) in {401, 403, 429}:
                    return finish_collection("GitHub trending", items, failures, errors, successful_requests)
                continue
            domain = _best_domain_for_repo(repo_json, config.domains)
            item = _repo_to_item(repo_json, domain, "trending")
            _mark_activity_window(item, config, target)
            items.append(item)
    return finish_collection("GitHub trending", items, failures, errors, successful_requests)


def _repo_to_item(repo: dict[str, Any], domain: DomainConfig, source_tag: str) -> DigestItem:
    full_name = repo.get("full_name") or ""
    html_url = repo.get("html_url") or f"https://github.com/{full_name}"
    description = repo.get("description") or ""
    topics = repo.get("topics") or []
    updated_at = repo.get("updated_at")
    pushed_at = repo.get("pushed_at")
    return DigestItem(
        id=full_name,
        source="github",
        item_type="repo",
        title=full_name,
        url=html_url,
        code_url=html_url,
        repo_description=description,
        published_at=repo.get("created_at"),
        updated_at=updated_at or pushed_at,
        source_tags=[source_tag, domain.name, *topics],
        categories=topics,
        language=repo.get("language"),
        stars=repo.get("stargazers_count"),
        forks=repo.get("forks_count"),
        quota_group=domain.quota_group,
        raw={
            "domain": domain.name,
            "pushed_at": pushed_at,
            "created_at": repo.get("created_at"),
            "updated_at": updated_at,
            "open_issues_count": repo.get("open_issues_count"),
        },
    )


def enrich_github_readmes(records: list[MaterialRecord], max_chars: int = 8000) -> list[MaterialRecord]:
    github_records = [record for record in records if record.source == "github" and not record.readme_excerpt]
    if not github_records:
        return records
    try:
        client = httpx.Client(timeout=20, follow_redirects=True, headers=_headers())
    except ImportError:
        return records
    with client:
        for record in github_records:
            try:
                response = client.get(f"{GITHUB_API}/repos/{record.title}/readme", headers={**_headers(), "Accept": "application/vnd.github.raw"})
                if response.status_code in {403, 404, 429}:
                    continue
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            record.readme_excerpt = _clean_readme(response.text, max_chars)
    return records


def enrich_github_update_signals(
    items: list[DigestItem],
    history: dict[str, SelectedRecord],
    max_repos: int = 10,
) -> list[DigestItem]:
    candidates = [item for item in items if item.source == "github" and item.canonical_key() in history]
    if not candidates or max_repos <= 0:
        return items
    try:
        client = httpx.Client(timeout=20, follow_redirects=True, headers=_headers())
    except ImportError:
        return items
    with client:
        for item in candidates[:max_repos]:
            if not _enrich_github_update_signal(client, item):
                break
    return items


def _enrich_github_update_signal(client: httpx.Client, item: DigestItem) -> bool:
    full_name = item.id
    try:
        release = client.get(f"{GITHUB_API}/repos/{full_name}/releases/latest")
        if release.status_code in {403, 429}:
            return False
        if release.status_code != 404:
            release.raise_for_status()
            payload = release.json()
            if payload.get("tag_name"):
                item.raw["latest_release_tag"] = str(payload["tag_name"])
            if payload.get("published_at"):
                item.raw["latest_release_published_at"] = str(payload["published_at"])
        tags = client.get(f"{GITHUB_API}/repos/{full_name}/tags", params={"per_page": 1})
        if tags.status_code in {403, 429}:
            return False
        if tags.status_code != 404:
            tags.raise_for_status()
            payload = tags.json()
            if isinstance(payload, list) and payload and payload[0].get("name"):
                item.raw["latest_tag_name"] = str(payload[0]["name"])
    except httpx.HTTPError:
        return True
    return True


def _clean_readme(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def _mark_activity_window(item: DigestItem, config: AppConfig, target: datetime) -> None:
    updated = _parse_datetime(item.updated_at) or _parse_datetime(item.raw.get("pushed_at"))
    if not updated:
        return
    age_days = (target - updated).days
    normal_days = int(config.sources.get("github", {}).get("normal_active_days", 30))
    high_days = int(config.sources.get("github", {}).get("high_relevance_active_days", 90))
    if age_days > normal_days and age_days <= high_days:
        item.is_historical_supplement = True
    elif age_days > high_days:
        item.score_breakdown["stale_penalty"] = -20


def _best_domain_for_repo(repo: dict[str, Any], domains: list[DomainConfig]) -> DomainConfig:
    haystack = " ".join(
        str(value or "")
        for value in [repo.get("full_name"), repo.get("description"), " ".join(repo.get("topics") or [])]
    ).lower()
    fallback = next((domain for domain in domains if domain.quota_group != "quantum"), domains[0])
    best = fallback
    best_hits = 0
    for domain in domains:
        hits = sum(1 for keyword in domain.include_keywords if keyword.lower() in haystack)
        if hits > best_hits:
            best = domain
            best_hits = hits
    return best


def _parse_trending_repos(html: str) -> list[str]:
    matches = re.findall(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', html)
    repos: list[str] = []
    seen: set[str] = set()
    for match in matches:
        if match.endswith("/stargazers") or match.endswith("/forks"):
            continue
        if match not in seen:
            seen.add(match)
            repos.append(match)
    return repos


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
