from datetime import datetime, timezone

import httpx

from daily_agent.config import load_config
from daily_agent.connectors.neurips import fetch_neurips


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def test_neurips_limits_detail_page_fetches(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["neurips"] = {
        "enabled": True,
        "years": [2025],
        "max_results_per_year": 5,
        "max_detail_pages_per_year": 2,
        "timeout_seconds": 15,
    }
    detail_urls: list[str] = []

    def handler(url, **kwargs):
        if url.endswith("/paper_files/paper/2025"):
            html = "".join(
                f'<a href="/paper_files/paper/2025/hash/paper-{index}-Abstract.html">Interesting Paper {index} Abstract</a>'
                for index in range(5)
            )
            return httpx.Response(200, request=httpx.Request("GET", url), text=html)
        detail_urls.append(url)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text="<h4>Abstract</h4><p>quantum circuit method</p><i>Alice</i><a href='paper.pdf'>Paper</a>",
        )

    monkeypatch.setattr("daily_agent.connectors.neurips.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_neurips(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=365)

    assert len(items) == 5
    assert len(detail_urls) == 2
    assert items[0].abstract == "quantum circuit method"
    assert items[-1].abstract is None
