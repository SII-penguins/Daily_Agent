from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
import yaml

import httpx


ROOT = Path(__file__).resolve().parents[1]


def _copy_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ["sources.yaml", "interests.yaml", "delivery.yaml", "feedback.yaml"]:
        shutil.copy(ROOT / "config" / name, config_dir / name)
    return tmp_path


def test_load_config_merges_keywords_expand_when_query_expansion_enabled(tmp_path):
    from daily_agent.config import load_config

    root = _copy_config(tmp_path)
    interests_path = root / "config" / "interests.yaml"
    sources_path = root / "config" / "sources.yaml"
    interests = yaml.safe_load(interests_path.read_text(encoding="utf-8"))
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    interests["domains"][0]["keywords"]["include"] = ["hardware-aware compilation"]
    interests["domains"][0]["keywords"]["expand"] = ["topology aware transpilation", "noise-aware routing"]
    sources["query_expansion"] = {"enabled": True, "max_expand_terms_per_domain": 2}
    interests_path.write_text(yaml.safe_dump(interests, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    config = load_config(root)
    domain = config.domains[0]

    assert domain.base_include_keywords == ["hardware-aware compilation"]
    assert domain.expanded_keywords == ["topology aware transpilation", "noise-aware routing"]
    assert domain.include_keywords == ["hardware-aware compilation", "topology aware transpilation", "noise-aware routing"]


def test_load_config_does_not_merge_expand_when_query_expansion_disabled(tmp_path):
    from daily_agent.config import load_config

    root = _copy_config(tmp_path)
    interests_path = root / "config" / "interests.yaml"
    sources_path = root / "config" / "sources.yaml"
    interests = yaml.safe_load(interests_path.read_text(encoding="utf-8"))
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    interests["domains"][0]["keywords"]["include"] = ["hardware-aware compilation"]
    interests["domains"][0]["keywords"]["expand"] = ["noise-aware routing"]
    sources["query_expansion"] = {"enabled": False, "max_expand_terms_per_domain": 10}
    interests_path.write_text(yaml.safe_dump(interests, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    config = load_config(root)
    domain = config.domains[0]

    assert domain.base_include_keywords == ["hardware-aware compilation"]
    assert domain.expanded_keywords == ["noise-aware routing"]
    assert domain.include_keywords == ["hardware-aware compilation"]


def test_openalex_uses_expanded_terms_for_search_queries(tmp_path, monkeypatch):
    from daily_agent.config import load_config
    from daily_agent.connectors.openalex import fetch_openalex

    root = _copy_config(tmp_path)
    interests_path = root / "config" / "interests.yaml"
    sources_path = root / "config" / "sources.yaml"
    interests = yaml.safe_load(interests_path.read_text(encoding="utf-8"))
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    interests["domains"] = [
        {
            "name": "quantum",
            "quota_group": "quantum",
            "priority": 1.0,
            "description": "",
            "keywords": {"include": ["quantum compilation"], "expand": ["noise-aware routing"]},
        }
    ]
    sources["query_expansion"] = {"enabled": True, "max_expand_terms_per_domain": 10}
    sources["openalex"] = {"enabled": True, "max_results_per_query": 1, "recent_days": 7, "timeout_seconds": 10}
    interests_path.write_text(yaml.safe_dump(interests, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = load_config(root)
    seen_queries: list[str] = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            seen_queries.append(params["search"])
            return Response()

    monkeypatch.setattr("daily_agent.connectors.openalex.httpx.Client", Client)

    fetch_openalex(config)

    assert "quantum compilation" in seen_queries
    assert "noise-aware routing" in seen_queries


def test_github_search_interleaves_expanded_terms_when_github_queries_exist(tmp_path, monkeypatch):
    from daily_agent.config import load_config
    from daily_agent.connectors.github import fetch_github

    root = _copy_config(tmp_path)
    interests_path = root / "config" / "interests.yaml"
    sources_path = root / "config" / "sources.yaml"
    interests = yaml.safe_load(interests_path.read_text(encoding="utf-8"))
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    interests["domains"] = [
        {
            "name": "agents",
            "quota_group": "exploratory",
            "priority": 1.0,
            "description": "",
            "keywords": {"include": ["llm agent"], "expand": ["tool-using agent", "browser agent"]},
            "github_queries": ["mcp server", "swe agent"],
        }
    ]
    sources["query_expansion"] = {"enabled": True, "max_expand_terms_per_domain": 10}
    sources["github"] = {
        "enabled": True,
        "search_enabled": True,
        "trending_enabled": False,
        "normal_active_days": 30,
        "max_results_per_query": 1,
        "max_queries_per_domain": 4,
    }
    interests_path.write_text(yaml.safe_dump(interests, allow_unicode=True, sort_keys=False), encoding="utf-8")
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config = load_config(root)
    seen_queries: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, **kwargs):
            seen_queries.append((params or {})["q"].split(" pushed:", 1)[0])
            return httpx.Response(200, request=httpx.Request("GET", url), json={"items": []})

    monkeypatch.setattr("daily_agent.connectors.github.httpx.Client", Client)

    fetch_github(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=30)

    assert seen_queries == ["mcp server", "tool-using agent", "swe agent", "browser agent"]


def test_scholarly_sources_do_not_fallback_to_github_queries():
    from daily_agent.config import DomainConfig
    from daily_agent.connectors.core import _queries as core_queries
    from daily_agent.connectors.crossref import _queries as crossref_queries
    from daily_agent.connectors.dblp import _queries as dblp_queries
    from daily_agent.connectors.google_scholar import _queries as google_scholar_queries
    from daily_agent.connectors.ieee import _queries as ieee_queries
    from daily_agent.connectors.openalex import _queries as openalex_queries
    from daily_agent.connectors.semantic_scholar import _queries as semantic_scholar_queries

    domain = DomainConfig(
        name="agent systems",
        quota_group="exploratory",
        priority=1.0,
        description="",
        include_keywords=[],
        exclude_keywords=[],
        arxiv_categories=[],
        github_queries=["mcp server stars:>100", "topic:agent language:python"],
    )

    assert google_scholar_queries(domain) == ["agent systems"]
    assert openalex_queries(domain) == ["agent systems"]
    assert semantic_scholar_queries(domain) == ["agent systems"]
    assert crossref_queries(domain) == ["agent systems"]
    assert core_queries(domain) == ["agent systems"]
    assert dblp_queries(domain) == ["agent systems"]
    assert ieee_queries(domain) == ["agent systems"]


def test_quality_and_enforce_full_include_query_expansion(tmp_path):
    from daily_agent.config import load_config
    from daily_agent.full_profile import enforce_full_profile
    from daily_agent.quality import run_quality_check

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["query_expansion"] = {"enabled": False, "max_expand_terms_per_domain": 2}
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = enforce_full_profile(root, write=True)
    config = load_config(root)
    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert result.changed is True
    assert config.sources["query_expansion"]["enabled"] is True
    assert config.sources["query_expansion"]["max_expand_terms_per_domain"] == 16
    assert by_key["query_expansion"].status == "full"
