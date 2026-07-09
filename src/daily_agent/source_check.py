from __future__ import annotations

import copy
import multiprocessing
import queue
from dataclasses import dataclass, field
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Callable

from daily_agent.config import AppConfig
from daily_agent.connectors.google_scholar import scholarly_runtime_enabled
from daily_agent.connectors import (
    fetch_arxiv,
    fetch_core,
    fetch_crossref,
    fetch_dblp,
    fetch_github,
    fetch_google_scholar,
    fetch_ieee,
    fetch_neurips,
    fetch_openalex,
    fetch_openreview,
    fetch_pmlr,
    fetch_semantic_scholar,
    scholarly_available,
)
from daily_agent.models import DigestItem
from daily_agent.secrets import credential_value


@dataclass
class SourceCheckResult:
    key: str
    name: str
    enabled: bool
    ok: bool
    skipped: bool = False
    item_count: int = 0
    credential_status: str = "not required"
    samples: list[str] = field(default_factory=list)
    error: str | None = None


def run_source_check(
    config: AppConfig,
    target_dt: datetime | None = None,
    window_days: int = 7,
    sample_limit: int = 3,
) -> list[SourceCheckResult]:
    target = target_dt or datetime.now(timezone.utc)
    probe_config = _probe_config(config, sample_limit)
    results: list[SourceCheckResult] = []
    for spec in _source_specs():
        source_config = config.sources.get(spec["key"], {}) or {}
        enabled = bool(source_config.get("enabled", True if spec["key"] in {"arxiv", "github"} else False))
        credential_status = _credential_status(spec["key"], source_config)
        if not enabled:
            results.append(SourceCheckResult(spec["key"], spec["name"], enabled=False, ok=True, skipped=True, credential_status="disabled"))
            continue
        if spec["key"] == "ieee" and credential_status.startswith("missing "):
            results.append(SourceCheckResult(spec["key"], spec["name"], enabled=True, ok=True, skipped=True, credential_status=credential_status))
            continue
        if spec["key"] == "google_scholar" and credential_status.startswith("missing "):
            results.append(SourceCheckResult(spec["key"], spec["name"], enabled=True, ok=True, skipped=True, credential_status=credential_status))
            continue
        if spec["key"] == "core" and credential_status.startswith("missing "):
            results.append(SourceCheckResult(spec["key"], spec["name"], enabled=True, ok=True, skipped=True, credential_status=credential_status))
            continue
        try:
            items = _fetch_for_source_check(spec, probe_config, source_config, credential_status, target, window_days)
            results.append(
                SourceCheckResult(
                    key=spec["key"],
                    name=spec["name"],
                    enabled=True,
                    ok=True,
                    item_count=len(items),
                    credential_status=credential_status,
                    samples=[item.title for item in items[:sample_limit]],
                )
            )
        except Exception as exc:
            results.append(
                SourceCheckResult(
                    key=spec["key"],
                    name=spec["name"],
                    enabled=True,
                    ok=False,
                    credential_status=credential_status,
                    error=str(exc),
                )
            )
    return results


def _fetch_for_source_check(
    spec: dict[str, str | Callable[[AppConfig, datetime, int], list[DigestItem]]],
    config: AppConfig,
    source_config: dict,
    credential_status: str,
    target: datetime,
    window_days: int,
) -> list[DigestItem]:
    fetcher = spec["fetcher"]
    assert callable(fetcher)
    if spec["key"] == "google_scholar" and credential_status == "scholarly package available":
        timeout = float(source_config.get("source_check_timeout_seconds", 15))
        return _fetch_with_process_timeout(fetcher, config, target, window_days, timeout)
    return fetcher(config, target, window_days=window_days)


def _fetch_with_process_timeout(
    fetcher: Callable[[AppConfig, datetime, int], list[DigestItem]],
    config: AppConfig,
    target: datetime,
    window_days: int,
    timeout_seconds: float,
) -> list[DigestItem]:
    if timeout_seconds <= 0 or "fork" not in multiprocessing.get_all_start_methods():
        return fetcher(config, target, window_days=window_days)
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_fetch_worker, args=(fetcher, config, target, window_days, result_queue))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        raise TimeoutError(f"source check timed out after {timeout_seconds:g}s")
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty:
        if process.exitcode:
            raise RuntimeError(f"source check worker exited with code {process.exitcode}")
        return []
    if status == "ok":
        return payload
    raise RuntimeError(str(payload))


def _fetch_worker(
    fetcher: Callable[[AppConfig, datetime, int], list[DigestItem]],
    config: AppConfig,
    target: datetime,
    window_days: int,
    result_queue,
) -> None:
    try:
        result_queue.put(("ok", fetcher(config, target, window_days=window_days)))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def render_source_check(results: list[SourceCheckResult], run_date: date, window_days: int) -> str:
    lines = [f"Source check: {run_date.isoformat()} window={window_days}d"]
    for result in results:
        if result.skipped:
            lines.append(f"- {result.name} skipped {result.credential_status}")
            continue
        if result.ok:
            line = f"- {result.name} ok {result.item_count} items"
            if result.credential_status != "not required":
                line += f" ({result.credential_status})"
            lines.append(line)
            if result.samples:
                lines.append(f"  samples: {'; '.join(result.samples)}")
        else:
            lines.append(f"- {result.name} failed {result.error or 'unknown error'}")
    return "\n".join(lines)


def _credential_status(key: str, source_config: dict) -> str:
    if key == "github":
        return "token present" if credential_value("GITHUB_TOKEN") or credential_value("GH_TOKEN") else "optional missing GITHUB_TOKEN/GH_TOKEN"
    if key == "semantic_scholar":
        env_name = str(source_config.get("api_key_env") or "SEMANTIC_SCHOLAR_API_KEY")
        return f"{env_name} present" if credential_value(env_name) else f"optional missing {env_name}"
    if key == "google_scholar":
        env_name = str(source_config.get("api_key_env") or "SERPAPI_API_KEY")
        if credential_value(env_name):
            return f"{env_name} present"
        if scholarly_runtime_enabled(source_config) and scholarly_available():
            return "scholarly package available"
        if source_config.get("scholarly_fallback_enabled", False) and scholarly_available():
            return f"missing {env_name}; scholarly runtime fallback disabled"
        return f"missing {env_name} and scholarly package"
    if key == "ieee":
        env_name = str(source_config.get("api_key_env") or "IEEE_XPLORE_API_KEY")
        return f"{env_name} present" if credential_value(env_name) else f"missing {env_name}"
    if key == "core":
        env_name = str(source_config.get("api_key_env") or "CORE_API_KEY")
        return f"{env_name} present" if credential_value(env_name) else f"missing {env_name}"
    return "not required"


def _probe_config(config: AppConfig, sample_limit: int) -> AppConfig:
    sources = copy.deepcopy(config.sources)
    per_query = max(1, min(int(sample_limit or 1), 3))
    for key, source_config in list(sources.items()):
        if not isinstance(source_config, dict):
            continue
        if "max_results_per_query" in source_config:
            source_config["max_results_per_query"] = per_query
        if "max_queries_per_domain" in source_config:
            source_config["max_queries_per_domain"] = 1
        if "timeout_seconds" in source_config:
            source_config["timeout_seconds"] = min(float(source_config.get("timeout_seconds") or 10), 10)
    if isinstance(sources.get("arxiv"), dict):
        sources["arxiv"]["request_delay_seconds"] = 0
        sources["arxiv"]["rate_limit_retries"] = 0
    if isinstance(sources.get("github"), dict):
        sources["github"]["max_results_per_query"] = per_query
        sources["github"]["trending_languages"] = (sources["github"].get("trending_languages") or [""])[:1]
    if isinstance(sources.get("google_scholar"), dict):
        sources["google_scholar"]["scholarly_max_results_per_query"] = per_query
    if isinstance(sources.get("openreview"), dict):
        sources["openreview"]["max_results_per_venue"] = per_query
        sources["openreview"]["venues"] = (sources["openreview"].get("venues") or [])[:1]
    if isinstance(sources.get("pmlr"), dict):
        sources["pmlr"]["max_results_per_volume"] = per_query
        sources["pmlr"]["volumes"] = (sources["pmlr"].get("volumes") or [])[:1]
    if isinstance(sources.get("neurips"), dict):
        sources["neurips"]["max_results_per_year"] = per_query
        sources["neurips"]["max_detail_pages_per_year"] = 1
        sources["neurips"]["years"] = (sources["neurips"].get("years") or [])[:1]
    return replace(config, sources=sources)


def _source_specs() -> list[dict[str, str | Callable[[AppConfig, datetime, int], list[DigestItem]]]]:
    return [
        {"key": "arxiv", "name": "arXiv", "fetcher": fetch_arxiv},
        {"key": "github", "name": "GitHub", "fetcher": fetch_github},
        {"key": "openalex", "name": "OpenAlex", "fetcher": fetch_openalex},
        {"key": "semantic_scholar", "name": "Semantic Scholar", "fetcher": fetch_semantic_scholar},
        {"key": "google_scholar", "name": "Google Scholar", "fetcher": fetch_google_scholar},
        {"key": "crossref", "name": "Crossref", "fetcher": fetch_crossref},
        {"key": "core", "name": "CORE", "fetcher": fetch_core},
        {"key": "dblp", "name": "DBLP", "fetcher": fetch_dblp},
        {"key": "ieee", "name": "IEEE", "fetcher": fetch_ieee},
        {"key": "openreview", "name": "OpenReview", "fetcher": fetch_openreview},
        {"key": "pmlr", "name": "PMLR", "fetcher": fetch_pmlr},
        {"key": "neurips", "name": "NeurIPS", "fetcher": fetch_neurips},
    ]
