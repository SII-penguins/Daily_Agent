from __future__ import annotations
from pathlib import Path

from datetime import datetime, timezone

from daily_agent.config import load_config


def test_github_connector_uses_configured_timeout(monkeypatch):
    from daily_agent.connectors import github

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.sources["github"]["timeout_seconds"] = 7

    monkeypatch.setattr(github.httpx, "Client", FakeClient)
    monkeypatch.setattr(github, "_fetch_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(github, "_fetch_trending", lambda *args, **kwargs: [])

    github.fetch_github(config, datetime(2026, 7, 9, tzinfo=timezone.utc), window_days=1)

    assert captured["timeout"] == 7
