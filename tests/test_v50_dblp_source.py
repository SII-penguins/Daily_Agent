from __future__ import annotations

from datetime import datetime, timezone

import httpx

from daily_agent.config import load_config
from daily_agent.models import DigestItem
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.source_check import run_source_check


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def test_dblp_fetch_normalizes_publication_search_json(monkeypatch):
    from daily_agent.connectors.dblp import fetch_dblp

    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["dblp"] = {"enabled": True, "max_results_per_query": 2, "recent_years": 2}

    def handler(url, **kwargs):
        assert url == "https://dblp.org/search/publ/api"
        assert kwargs["params"]["format"] == "json"
        assert kwargs["params"]["q"]
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "result": {
                    "hits": {
                        "hit": [
                            {
                                "@id": "conf/icml/Test26",
                                "info": {
                                    "title": "Hardware-Aware Quantum Circuit Compilation.",
                                    "authors": {"author": [{"text": "Alice A."}, {"text": "Bob B."}]},
                                    "venue": "ICML",
                                    "year": "2026",
                                    "type": "Conference and Workshop Papers",
                                    "key": "conf/icml/Test26",
                                    "doi": "10.5555/dblp.1",
                                    "url": "https://dblp.org/rec/conf/icml/Test26",
                                    "ee": "https://doi.org/10.5555/dblp.1",
                                },
                            }
                        ]
                    }
                }
            },
        )

    monkeypatch.setattr("daily_agent.connectors.dblp.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_dblp(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=30)

    assert len(items) == 1
    assert items[0].source == "dblp"
    assert items[0].item_type == "paper"
    assert items[0].title == "Hardware-Aware Quantum Circuit Compilation"
    assert items[0].doi == "10.5555/dblp.1"
    assert items[0].url == "https://dblp.org/rec/conf/icml/Test26"
    assert items[0].authors == ["Alice A.", "Bob B."]
    assert items[0].published_at == "2026-01-01"
    assert items[0].raw["dblp_key"] == "conf/icml/Test26"
    assert items[0].raw["venue"] == "ICML"
    assert items[0].raw["dblp_url"] == "https://dblp.org/rec/conf/icml/Test26"


def test_dblp_dedup_merges_by_doi_and_alias():
    arxiv = DigestItem(
        id="2605.00001",
        source="arxiv",
        item_type="paper",
        title="Hardware-Aware Quantum Circuit Compilation",
        url="https://arxiv.org/abs/2605.00001",
        arxiv_id="2605.00001",
        doi="10.5555/dblp.1",
    )
    dblp = DigestItem(
        id="conf/icml/Test26",
        source="dblp",
        item_type="paper",
        title="Hardware-Aware Quantum Circuit Compilation",
        url="https://dblp.org/rec/conf/icml/Test26",
        doi="10.5555/dblp.1",
        raw={"dblp_key": "conf/icml/Test26", "venue": "ICML"},
    )

    merged = deduplicate_items([dblp, arxiv])

    assert len(merged) == 1
    assert merged[0].canonical_key() == "arxiv:2605.00001"
    assert merged[0].raw["source_aliases"]["dblp"] == "conf/icml/Test26"
    assert "dblp" in merged[0].raw["evidence"]["sources"]


def test_source_check_includes_dblp(monkeypatch, tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["dblp"] = {"enabled": True}

    monkeypatch.setattr("daily_agent.source_check.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_github", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openalex", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_semantic_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_google_scholar", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_crossref", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_ieee", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_openreview", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_pmlr", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.fetch_neurips", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr(
        "daily_agent.source_check.fetch_dblp",
        lambda config, target_dt, window_days=None: [DigestItem(id="conf/icml/Test26", source="dblp", item_type="paper", title="DBLP paper", url="https://dblp.org/rec/conf/icml/Test26")],
    )

    results = run_source_check(config, datetime(2026, 5, 19, tzinfo=timezone.utc), window_days=7)
    by_key = {result.key: result for result in results}

    assert by_key["dblp"].ok is True
    assert by_key["dblp"].item_count == 1
    assert by_key["dblp"].credential_status == "not required"
