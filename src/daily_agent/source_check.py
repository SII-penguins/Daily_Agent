from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from daily_agent.config import AppConfig
from daily_agent.connectors import (
    fetch_arxiv,
    fetch_crossref,
    fetch_github,
    fetch_ieee,
    fetch_neurips,
    fetch_openalex,
    fetch_openreview,
    fetch_pmlr,
    fetch_semantic_scholar,
)
from daily_agent.models import DigestItem


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
        try:
            items = spec["fetcher"](config, target, window_days=window_days)
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
        return "token present" if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") else "optional missing GITHUB_TOKEN/GH_TOKEN"
    if key == "semantic_scholar":
        env_name = str(source_config.get("api_key_env") or "SEMANTIC_SCHOLAR_API_KEY")
        return f"{env_name} present" if os.environ.get(env_name) else f"optional missing {env_name}"
    if key == "ieee":
        env_name = str(source_config.get("api_key_env") or "IEEE_XPLORE_API_KEY")
        return f"{env_name} present" if os.environ.get(env_name) else f"missing {env_name}"
    return "not required"


def _source_specs() -> list[dict[str, str | Callable[[AppConfig, datetime, int], list[DigestItem]]]]:
    return [
        {"key": "arxiv", "name": "arXiv", "fetcher": fetch_arxiv},
        {"key": "github", "name": "GitHub", "fetcher": fetch_github},
        {"key": "openalex", "name": "OpenAlex", "fetcher": fetch_openalex},
        {"key": "semantic_scholar", "name": "Semantic Scholar", "fetcher": fetch_semantic_scholar},
        {"key": "crossref", "name": "Crossref", "fetcher": fetch_crossref},
        {"key": "ieee", "name": "IEEE", "fetcher": fetch_ieee},
        {"key": "openreview", "name": "OpenReview", "fetcher": fetch_openreview},
        {"key": "pmlr", "name": "PMLR", "fetcher": fetch_pmlr},
        {"key": "neurips", "name": "NeurIPS", "fetcher": fetch_neurips},
    ]
