from pathlib import Path
from datetime import datetime, timezone

import httpx

from daily_agent.config import load_config
from daily_agent.connectors.crossref import fetch_crossref


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def test_crossref_limits_queries_per_domain(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["crossref"] = {
        "enabled": True,
        "recent_days": 7,
        "max_results_per_query": 1,
        "max_queries_per_domain": 1,
        "timeout_seconds": 45,
    }
    seen_queries: list[str] = []

    def handler(url, **kwargs):
        seen_queries.append(kwargs["params"]["query.bibliographic"])
        return httpx.Response(200, request=httpx.Request("GET", url), json={"message": {"items": []}})

    monkeypatch.setattr("daily_agent.connectors.crossref.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    fetch_crossref(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=30)

    assert len(seen_queries) == len(config.domains)
