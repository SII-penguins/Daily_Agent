from pathlib import Path
from datetime import datetime, timezone

from daily_agent.config import load_config
from daily_agent.connectors.arxiv import fetch_arxiv


def test_arxiv_limits_queries_per_domain_before_rate_limit_sleep(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["arxiv"] = {
        "enabled": True,
        "recent_days": 7,
        "max_results_per_query": 1,
        "request_delay_seconds": 3,
        "max_queries_per_domain": 2,
    }
    queries: list[str] = []
    sleeps: list[float] = []

    def fake_fetch_query(search_query, domain, target, recent_days, query_window_days, max_results, source_config):
        queries.append(f"{domain.name}:{search_query}")
        return []

    monkeypatch.setattr("daily_agent.connectors.arxiv._fetch_query", fake_fetch_query)
    monkeypatch.setattr("daily_agent.connectors.arxiv.time.sleep", lambda seconds: sleeps.append(seconds))

    fetch_arxiv(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=7)

    assert len(queries) == 2 * len(config.domains)
    assert all(sleep == 3 for sleep in sleeps)
    assert len(sleeps) == len(queries)
